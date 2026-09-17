#!/usr/bin/env python3
"""Small, deterministic lifecycle recorder for Claude Code and Codex.

Lifecycle receipts remain metadata-only: this hook does not persist prompts,
tool inputs, tool outputs, or compact summaries.  A separate local index worker
may store redacted user/assistant text for evidence retrieval; promotion stays
human-reviewed.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


INBOX = Path(
    os.environ.get("AGENT_HOOK_INBOX", str(Path.home() / ".agent-hooks/inbox"))
)
HARNESS = os.environ.get("AGENT_HOOK_HARNESS", "unknown")
EVENT = os.environ.get("AGENT_HOOK_EVENT", "")
MEMORY_SCRIPT = Path(__file__).with_name("session_memory.py")
CAPTURE_SCRIPT = Path(__file__).with_name("learning_review.py")
CAPTURE_OUTBOX = Path(
    os.environ.get(
        "AGENT_HOOK_CAPTURE_OUTBOX",
        os.environ.get("AGENT_HOOK_OUTBOX", str(Path.home() / ".agent-hooks/outbox")),
    )
)


def read_payload():
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def field(payload, *names, default=None):
    for name in names:
        value = payload.get(name)
        if value is not None:
            return value
    return default


def safe_text(value, limit=400):
    if not isinstance(value, str):
        return None
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    return value[:limit] if value else None


def safe_id(value):
    value = safe_text(value, 100) or "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def lifecycle_record(payload, event):
    """Write metadata only; never copy transcript or tool content here."""
    transcript_path = safe_text(field(payload, "transcript_path", "transcriptPath"), 2000)
    if field(payload, "agent_type", "agentType", "subagent") or (
        transcript_path and "/subagents/" in transcript_path
    ):
        return
    try:
        INBOX.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(INBOX, 0o700)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        session_id = safe_id(field(payload, "session_id", "sessionId"))
        record = {
            "schema_version": 1,
            "status": "pending_review",
            "harness": HARNESS,
            "event": event,
            "record_kind": "checkpoint" if event == "PreCompact" else "session_end",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "cwd": safe_text(field(payload, "cwd", "working_directory"), 1000),
            "transcript_path": transcript_path,
            "reason": safe_text(field(payload, "reason", "trigger"), 100),
            "suggested_destinations": [
                "repo CLAUDE.md or AGENTS.md for reusable repository rules",
                "OpenKnowledge for human-reviewed durable knowledge",
            ],
        }
        suffix = f"{time.time_ns()}"
        if event == "PreCompact":
            path = INBOX / f"checkpoint-{HARNESS}-{session_id}.json"
            temp_path = INBOX / f".{path.name}.{suffix}.tmp"
        else:
            path = INBOX / f"{timestamp}-{HARNESS}-{event or 'event'}-{session_id}-{suffix}.json"
            temp_path = path
        with temp_path.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temp_path, 0o600)
        if temp_path != path:
            temp_path.replace(path)
    except (OSError, ValueError):
        # Lifecycle capture must never interfere with the agent.
        return


def start_memory_index(payload, event):
    """Start a local derived-index worker without delaying the lifecycle hook."""
    if not MEMORY_SCRIPT.is_file():
        return
    transcript_path = safe_text(field(payload, "transcript_path", "transcriptPath"), 2000)
    args = [sys.executable, str(MEMORY_SCRIPT), "index", "--harness", HARNESS]
    if transcript_path:
        args.extend(["--path", transcript_path])
    else:
        args.extend(["--limit", "2", "--max-seconds", "2"])
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        # Indexing is advisory and must never interfere with the agent.
        return


def start_report_capture(payload, event):
    """Capture a marked completed report after SessionEnd without blocking."""
    if event != "SessionEnd" or os.environ.get("AGENT_HOOK_CAPTURE", "1").lower() in {
        "0",
        "false",
        "no",
    }:
        return
    if not CAPTURE_SCRIPT.is_file():
        return
    transcript_path = safe_text(field(payload, "transcript_path", "transcriptPath"), 2000)
    if not transcript_path:
        return
    args = [
        sys.executable,
        str(CAPTURE_SCRIPT),
        "capture",
        "--session-path",
        transcript_path,
        "--outbox",
        str(CAPTURE_OUTBOX),
        "--harness",
        HARNESS,
    ]
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        # Report capture is advisory and must never interfere with the agent.
        return


def memory_status():
    """Return a short local-index status line for SessionStart."""
    if not MEMORY_SCRIPT.is_file():
        return None
    try:
        result = subprocess.run(
            [sys.executable, str(MEMORY_SCRIPT), "status", "--format", "hook"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value[:400] if result.returncode == 0 and value else None


def scan_inbox():
    """Classify inbox files without changing them.

    The lifecycle hook is deliberately fail-open.  A scan failure must still
    be distinguishable from an empty inbox, and incomplete records must not be
    counted as reviewable work or silently deleted.
    """
    result = {
        "file_count": 0,
        "reviewable_records": 0,
        "reviewable_sessions": set(),
        "pending_sessions": {},
        "missing_transcript": 0,
        "unknown_transcript": 0,
        "malformed": 0,
        "scan_error": False,
    }
    try:
        candidates = sorted(INBOX.glob("*.json"))
    except OSError:
        result["scan_error"] = True
        return result
    result["file_count"] = len(candidates)
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            result["malformed"] += 1
            continue
        if not isinstance(record, dict):
            result["malformed"] += 1
            continue
        if record.get("status") != "pending_review":
            continue

        session_id = record.get("session_id")
        session_key = session_id if isinstance(session_id, str) and session_id else path.name
        result["pending_sessions"][session_key] = (
            result["pending_sessions"].get(session_key, 0) + 1
        )
        transcript_path = record.get("transcript_path")
        if not isinstance(transcript_path, str) or not transcript_path.strip():
            result["unknown_transcript"] += 1
            continue
        try:
            Path(transcript_path).stat()
        except FileNotFoundError:
            result["missing_transcript"] += 1
            continue
        except OSError:
            result["missing_transcript"] += 1
            continue

        result["reviewable_records"] += 1
        result["reviewable_sessions"].add(session_key)

    return result


def session_start_notice():
    start_memory_index({}, "SessionStart")
    scan = scan_inbox()
    memory_line = memory_status()
    if not scan["file_count"] and not scan["scan_error"] and not memory_line:
        return

    if scan["scan_error"]:
        message = f"Agent hooks: unable to scan {INBOX}; inbox state is unknown."
    else:
        duplicate_sessions = sum(
            count > 1 for count in scan["pending_sessions"].values()
        )
        parts = [
            f"{len(scan['reviewable_sessions'])} reviewable pending session(s) from "
            f"{scan['reviewable_records']} lifecycle record(s)"
        ]
        if scan["unknown_transcript"]:
            parts.append(
                f"{scan['unknown_transcript']} pending record(s) lack a transcript path"
            )
        if scan["missing_transcript"]:
            parts.append(
                f"{scan['missing_transcript']} pending record(s) reference an unavailable transcript"
            )
        if scan["malformed"]:
            parts.append(f"{scan['malformed']} malformed inbox file(s)")
        if duplicate_sessions:
            parts.append(
                f"{duplicate_sessions} session(s) have multiple lifecycle records; deduplicate during review"
            )
        message = f"Agent hooks: {'; '.join(parts)} in {INBOX}; review before promotion."
    if memory_line:
        message = f"{message} {memory_line}" if message else memory_line

    sys.stderr.write(message + "\n")
    # Bare stderr on a zero-exit hook is silently discarded by the harness; the
    # notice only actually reaches the user/model via stdout JSON.
    print(json.dumps({
        "systemMessage": message,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": message,
        },
    }))


def main():
    payload = read_payload()
    payload_event = field(payload, "hook_event_name", "hookEventName")
    event = EVENT or payload_event or ""
    if event in {"PreCompact", "SessionEnd"}:
        lifecycle_record(payload, event)
        start_memory_index(payload, event)
        start_report_capture(payload, event)
        return
    if event == "SessionStart":
        session_start_notice()


if __name__ == "__main__":
    main()
