#!/usr/bin/env python3
"""Capture marked agent learning-review reports and safely prune approved inbox files.

The capture path is deliberately deterministic and does not interpret a report.
The approval skill owns semantic review, human decisions, durable writes, and the
decision to prepare a deletion manifest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path


MARKER = os.environ.get("AGENT_LEARNING_MARKER", "AGENT_LEARNING_REVIEW_V1")
SCHEMA_VERSION = 1
DEFAULT_SESSIONS_ROOT = Path.home() / ".codex/sessions"
DEFAULT_OUTBOX = Path(
    os.environ.get("AGENT_HOOK_OUTBOX", str(Path.home() / ".agent-hooks/outbox"))
)
DEFAULT_INBOX = Path(
    os.environ.get("AGENT_HOOK_INBOX", str(Path.home() / ".agent-hooks/inbox"))
)
SESSION_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def has_terminal_marker(report: str) -> bool:
    return report.rstrip().endswith(MARKER)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_name(value: str) -> str:
    result = SESSION_ID_RE.sub("-", value).strip("-.")
    return result[:160] or "unknown-session"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_state(outbox: Path) -> dict:
    path = outbox / "capture-state.json"
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "captures": {}, "scanned": {}}
    value = load_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"invalid capture state: {path}")
    captures = value.get("captures")
    if not isinstance(captures, dict):
        raise ValueError(f"invalid capture state captures: {path}")
    scanned = value.setdefault("scanned", {})
    if not isinstance(scanned, dict):
        raise ValueError(f"invalid capture state scanned: {path}")
    return value


def save_state(outbox: Path, state: dict) -> None:
    atomic_write(
        outbox / "capture-state.json",
        (json.dumps(state, indent=2, sort_keys=True) + "\n").encode(),
    )


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def extract_final(path: Path) -> tuple[str, str] | None:
    session_id = ""
    final_text = ""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = (
                record.get("sessionId")
                or record.get("session_id")
                or session_id
            )
            payload = record.get("payload")
            if record.get("type") == "session_meta" and isinstance(payload, dict):
                session_id = payload.get("session_id") or payload.get("id") or session_id
            if record.get("type") == "response_item" and isinstance(payload, dict):
                if payload.get("type") != "message" or payload.get("role") != "assistant":
                    continue
                if payload.get("phase") != "final_answer":
                    continue
                text = content_text(payload.get("content"))
                if text:
                    final_text = text
                continue

            # Claude Code stores assistant messages as top-level records with a
            # nested message object rather than Codex response_item records.
            if record.get("type") == "assistant":
                message = record.get("message")
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                text = content_text(message.get("content"))
                if text:
                    final_text = text
    if not final_text:
        return None
    return session_id or path.stem, final_text


def report_paths(outbox: Path) -> list[Path]:
    runs = outbox / "runs"
    if not runs.exists():
        return []
    return sorted(runs.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)


def capture_one(
    path: Path,
    outbox: Path,
    allow_unmarked: bool = False,
    harness: str = "unknown",
) -> dict:
    if not path.is_file() or path.suffix != ".jsonl":
        return {"status": "ignored", "path": str(path), "reason": "not_jsonl"}
    extracted = extract_final(path)
    if extracted is None:
        return {"status": "ignored", "path": str(path), "reason": "no_final_answer"}
    session_id, report = extracted
    if not has_terminal_marker(report) and not allow_unmarked:
        return {"status": "ignored", "path": str(path), "reason": "marker_missing"}

    source_sha = sha256_file(path)
    report_sha = sha256_bytes(report.encode())
    capture_key = f"{session_id}:{report_sha}"
    state = load_state(outbox)
    if capture_key in state["captures"]:
        return {
            "status": "already_captured",
            "path": str(path),
            "session_id": session_id,
            "report_sha256": report_sha,
            "artifact": state["captures"][capture_key]["artifact"],
        }

    captured_at = iso_now()
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "status": "pending_approval",
        "run_id": session_id,
        "session_id": session_id,
        "harness": harness,
        "source_path": str(path.resolve()),
        "source_sha256": source_sha,
        "source_size": path.stat().st_size,
        "source_mtime_ns": path.stat().st_mtime_ns,
        "report_sha256": report_sha,
        "captured_at": captured_at,
        "marker_required": not allow_unmarked,
    }
    artifact = outbox / "runs" / f"{safe_name(session_id)}-{report_sha[:16]}.json"
    body = "<!-- agent-learning-review-capture-v1\n"
    body += json.dumps(metadata, sort_keys=True)
    body += "\n-->\n\n"
    body += report.rstrip() + "\n"
    atomic_write(artifact.with_suffix(".md"), body.encode())
    atomic_write(artifact, (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode())
    state["captures"][capture_key] = {
        "artifact": str(artifact),
        "report_path": str(artifact.with_suffix(".md")),
        "captured_at": captured_at,
        "source_path": str(path.resolve()),
        "source_sha256": source_sha,
        "report_sha256": report_sha,
    }
    save_state(outbox, state)
    return {
        "status": "captured",
        "path": str(path),
        "session_id": session_id,
        "report_sha256": report_sha,
        "artifact": str(artifact.with_suffix(".md")),
    }


def candidate_session_files(root: Path, lookback_hours: float) -> list[Path]:
    cutoff = dt.datetime.now().timestamp() - lookback_hours * 3600
    paths = []
    for path in root.rglob("*.jsonl"):
        try:
            if path.is_file() and path.stat().st_mtime >= cutoff:
                paths.append(path)
        except OSError:
            continue
    return sorted(paths)


def capture(args: argparse.Namespace) -> dict:
    outbox = Path(args.outbox).expanduser().resolve()
    if args.session_path:
        paths = [Path(args.session_path).expanduser().resolve()]
    else:
        root = Path(args.sessions_root).expanduser().resolve()
        paths = candidate_session_files(root, args.lookback_hours) if root.exists() else []
    state = load_state(outbox)
    candidates = []
    for path in paths:
        try:
            signature = f"{path.stat().st_size}:{path.stat().st_mtime_ns}"
        except OSError:
            continue
        if state["scanned"].get(str(path)) == signature:
            continue
        candidates.append((path, signature))

    results = [
        capture_one(path, outbox, args.allow_unmarked, args.harness)
        for path, _ in candidates
    ]
    state = load_state(outbox)
    for path, signature in candidates:
        state["scanned"][str(path)] = signature
    save_state(outbox, state)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "sessions_root": str(Path(args.sessions_root).expanduser().resolve()),
        "outbox": str(outbox),
        "examined": len(candidates),
        "unchanged_skipped": len(paths) - len(candidates),
        "captured": sum(result["status"] == "captured" for result in results),
        "already_captured": sum(result["status"] == "already_captured" for result in results),
        "ignored": sum(result["status"] == "ignored" for result in results),
    }
    if args.verbose:
        result["results"] = results
    else:
        result["captured_results"] = [
            item for item in results if item["status"] in {"captured", "already_captured"}
        ]
    return result


def list_reports(args: argparse.Namespace) -> dict:
    outbox = Path(args.outbox).expanduser().resolve()
    reports = []
    for metadata_path in report_paths(outbox):
        try:
            metadata = load_json(metadata_path)
        except (OSError, json.JSONDecodeError):
            reports.append({"status": "invalid", "metadata": str(metadata_path)})
            continue
        if isinstance(metadata, dict):
            report_path = metadata_path.with_suffix(".md")
            valid = True
            if metadata.get("marker_required"):
                try:
                    body = report_path.read_text(encoding="utf-8")
                    valid = has_terminal_marker(body)
                except OSError:
                    valid = False
            metadata = dict(metadata)
            metadata["capture_valid"] = valid
            if not valid:
                metadata["status"] = "invalid_capture"
            reports.append(metadata)
    return {"schema_version": SCHEMA_VERSION, "outbox": str(outbox), "reports": reports}


def record_decision(args: argparse.Namespace) -> dict:
    if args.decision not in {"apply", "reject", "defer", "promoted", "failed"}:
        raise ValueError("unsupported decision")
    outbox = Path(args.outbox).expanduser().resolve()
    event = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": iso_now(),
        "run_id": args.run_id,
        "report_sha256": args.report_sha256,
        "candidate_id": args.candidate_id,
        "decision": args.decision,
    }
    path = outbox / "decisions.jsonl"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(canonical_json(event) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {"status": "recorded", "path": str(path), "event": event}


def direct_inbox_child(path: Path, inbox: Path) -> Path:
    resolved = path.expanduser().resolve()
    expected_parent = inbox.expanduser().resolve()
    if resolved.parent != expected_parent or resolved.suffix != ".json":
        raise ValueError(f"refusing non-inbox JSON path: {path}")
    return resolved


def prepare_prune(args: argparse.Namespace) -> dict:
    inbox = Path(args.inbox).expanduser().resolve()
    if not args.path:
        raise ValueError("at least one exact --path is required")
    entries = []
    for raw_path in args.path:
        path = direct_inbox_child(Path(raw_path), inbox)
        if not path.exists() or not path.is_file():
            raise ValueError(f"inbox path is not a file: {path}")
        entries.append({"path": str(path), "sha256": sha256_file(path)})
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "inbox": str(inbox),
        "created_at": iso_now(),
        "paths": sorted(entries, key=lambda entry: entry["path"]),
    }
    output = Path(args.output).expanduser().resolve()
    atomic_write(output, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    return {
        "status": "prepared",
        "manifest": str(output),
        "confirm_digest": sha256_bytes(canonical_json(manifest)),
        "path_count": len(entries),
        "paths": entries,
    }


def prune(args: argparse.Namespace) -> dict:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid prune manifest")
    expected_digest = sha256_bytes(canonical_json(manifest))
    if args.confirm != expected_digest:
        raise ValueError("confirmation digest does not match the manifest")
    inbox = Path(args.inbox).expanduser().resolve()
    if manifest.get("inbox") != str(inbox):
        raise ValueError("manifest inbox does not match requested inbox")
    raw_paths = manifest.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("prune manifest has no paths")

    checked: list[tuple[Path, str]] = []
    for entry in raw_paths:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("invalid prune path entry")
        path = direct_inbox_child(Path(entry["path"]), inbox)
        expected_sha = entry.get("sha256")
        if not isinstance(expected_sha, str) or not path.is_file():
            raise ValueError(f"prune preflight failed: {path}")
        actual_sha = sha256_file(path)
        if actual_sha != expected_sha:
            raise ValueError(f"prune preflight hash mismatch: {path}")
        checked.append((path, expected_sha))

    for path, _ in checked:
        path.unlink()
    remaining = [str(path) for path, _ in checked if path.exists()]
    if remaining:
        raise RuntimeError(f"prune verification failed: {remaining}")
    return {
        "status": "pruned",
        "manifest": str(manifest_path),
        "confirm_digest": expected_digest,
        "deleted": [str(path) for path, _ in checked],
        "remaining_deleted_paths": remaining,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--sessions-root", default=str(DEFAULT_SESSIONS_ROOT))
    capture_parser.add_argument("--outbox", default=str(DEFAULT_OUTBOX))
    capture_parser.add_argument("--lookback-hours", type=float, default=72)
    capture_parser.add_argument("--session-path")
    capture_parser.add_argument("--harness", default="unknown")
    capture_parser.add_argument("--allow-unmarked", action="store_true")
    capture_parser.add_argument("--verbose", action="store_true")

    list_parser = sub.add_parser("list")
    list_parser.add_argument("--outbox", default=str(DEFAULT_OUTBOX))

    decision_parser = sub.add_parser("record-decision")
    decision_parser.add_argument("--outbox", default=str(DEFAULT_OUTBOX))
    decision_parser.add_argument("--run-id", required=True)
    decision_parser.add_argument("--report-sha256", required=True)
    decision_parser.add_argument("--candidate-id", required=True)
    decision_parser.add_argument("--decision", required=True)

    prepare_parser = sub.add_parser("prepare-prune")
    prepare_parser.add_argument("--inbox", default=str(DEFAULT_INBOX))
    prepare_parser.add_argument("--output", required=True)
    prepare_parser.add_argument("--path", action="append", required=True)

    prune_parser = sub.add_parser("prune")
    prune_parser.add_argument("--inbox", default=str(DEFAULT_INBOX))
    prune_parser.add_argument("--manifest", required=True)
    prune_parser.add_argument("--confirm", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "capture":
        result = capture(args)
    elif args.command == "list":
        result = list_reports(args)
    elif args.command == "record-decision":
        result = record_decision(args)
    elif args.command == "prepare-prune":
        result = prepare_prune(args)
    elif args.command == "prune":
        result = prune(args)
    else:
        raise ValueError(f"unknown command: {args.command}")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"agent-learning-review: {exc}", file=sys.stderr)
        raise SystemExit(1)
