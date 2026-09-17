import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "hooks" / "learning_review.py"
GOVERNANCE = Path(__file__).parents[1] / "hooks" / "agent_governance.py"
SPEC = importlib.util.spec_from_file_location("learning_review", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def codex_line(record_type, payload):
    return json.dumps({"type": record_type, "payload": payload}) + "\n"


class LearningReviewTests(unittest.TestCase):
    def make_rollout(self, root: Path, session_id: str, report: str, marker=True) -> Path:
        path = root / f"rollout-{session_id}.jsonl"
        final = report + (f"\n{MODULE.MARKER}\n" if marker else "")
        path.write_text(
            codex_line("session_meta", {"id": session_id, "session_id": session_id})
            + codex_line(
                "response_item",
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": final}],
                },
            ),
            encoding="utf-8",
        )
        return path

    def test_capture_requires_marker_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            outbox = root / "outbox"
            sessions.mkdir()
            marked = self.make_rollout(sessions, "session-1", "## Run summary")
            unmarked = self.make_rollout(sessions, "session-2", "## Run summary", marker=False)

            first = MODULE.capture_one(marked, outbox)
            second = MODULE.capture_one(marked, outbox)
            ignored = MODULE.capture_one(unmarked, outbox)

            self.assertEqual(first["status"], "captured")
            self.assertEqual(second["status"], "already_captured")
            self.assertEqual(ignored["reason"], "marker_missing")
            self.assertEqual(len(MODULE.report_paths(outbox)), 1)
            metadata = json.loads(Path(first["artifact"]).with_suffix(".json").read_text())
            self.assertEqual(metadata["status"], "pending_approval")
            self.assertEqual(metadata["session_id"], "session-1")

    def test_marker_inside_prose_is_not_a_capture_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            outbox = root / "outbox"
            sessions.mkdir()
            path = self.make_rollout(
                sessions,
                "session-prose-marker",
                f"## Run summary\nThe required marker is {MODULE.MARKER}.",
                marker=False,
            )
            result = MODULE.capture_one(path, outbox)
            self.assertEqual(result["reason"], "marker_missing")

    def test_claude_transcript_is_captured_by_the_session_end_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            outbox = root / "outbox"
            memory_db = root / "memory.sqlite3"
            inbox.mkdir()
            transcript = root / "claude-session.jsonl"
            report = "## Run summary\nA marked Claude review report."
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": "claude-session-1",
                                "message": {"role": "user", "content": "Review"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "sessionId": "claude-session-1",
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": report + f"\n{MODULE.MARKER}\n",
                                        }
                                    ],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "AGENT_HOOK_INBOX": str(inbox),
                    "AGENT_HOOK_CAPTURE_OUTBOX": str(outbox),
                    "AGENT_HOOK_HARNESS": "claude",
                    "AGENT_HOOK_EVENT": "SessionEnd",
                    "AGENT_SESSION_MEMORY_DB": str(memory_db),
                    "AGENT_SESSION_MEMORY_CLAUDE_ROOT": str(root),
                    "AGENT_SESSION_MEMORY_CODEX_ROOT": str(root),
                }
            )
            result = subprocess.run(
                [sys.executable, str(GOVERNANCE)],
                input=json.dumps(
                    {
                        "session_id": "claude-session-1",
                        "transcript_path": str(transcript),
                    }
                ),
                env=environment,
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            self.assertEqual(result.returncode, 0)
            metadata_paths = []
            for _ in range(30):
                metadata_paths = list(MODULE.report_paths(outbox))
                if metadata_paths:
                    break
                time.sleep(0.05)
            self.assertEqual(len(metadata_paths), 1)
            metadata = json.loads(metadata_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(metadata["harness"], "claude")
            self.assertEqual(metadata["session_id"], "claude-session-1")
            body = metadata_paths[0].with_suffix(".md").read_text(encoding="utf-8")
            self.assertIn(report, body)

    def test_legacy_capture_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            outbox = root / "outbox"
            sessions.mkdir()
            legacy = self.make_rollout(sessions, "legacy", "## Run summary", marker=False)
            result = MODULE.capture_one(legacy, outbox)
            self.assertEqual(result["status"], "ignored")
            result = MODULE.capture_one(legacy, outbox, allow_unmarked=True)
            self.assertEqual(result["status"], "captured")

    def test_prune_manifest_requires_exact_hash_and_confirms_all_before_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            outbox = root / "outbox"
            inbox.mkdir()
            first = inbox / "one.json"
            second = inbox / "two.json"
            first.write_text('{"status":"pending_review"}\n')
            second.write_text('{"status":"pending_review"}\n')
            manifest_path = outbox / "prune.json"

            args = type(
                "Args",
                (),
                {
                    "inbox": str(inbox),
                    "output": str(manifest_path),
                    "path": [str(first), str(second)],
                },
            )
            prepared = MODULE.prepare_prune(args)
            self.assertEqual(prepared["path_count"], 2)

            first.write_text('{"status":"changed"}\n')
            prune_args = type(
                "Args",
                (),
                {
                    "inbox": str(inbox),
                    "manifest": str(manifest_path),
                    "confirm": prepared["confirm_digest"],
                },
            )
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                MODULE.prune(prune_args)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_prune_rejects_paths_outside_inbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            inbox.mkdir()
            outside = root / "outside.json"
            outside.write_text("{}\n")
            args = type(
                "Args",
                (),
                {
                    "inbox": str(inbox),
                    "output": str(root / "manifest.json"),
                    "path": [str(outside)],
                },
            )
            with self.assertRaisesRegex(ValueError, "non-inbox"):
                MODULE.prepare_prune(args)


if __name__ == "__main__":
    unittest.main()
