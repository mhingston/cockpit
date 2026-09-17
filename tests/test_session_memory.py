#!/usr/bin/env python3
"""E2E tests for the local session evidence index and lifecycle hook."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).parents[1] / "hooks" / "session_memory.py"
GOVERNANCE = Path(__file__).parents[1] / "hooks" / "agent_governance.py"


class SessionMemoryE2E(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="agent-session-memory-test-")
        self.root = Path(self.temp_dir.name)
        self.codex_root = self.root / "codex"
        self.claude_root = self.root / "claude"
        self.inbox = self.root / "inbox"
        self.db = self.root / "memory.sqlite3"
        self.codex_root.mkdir()
        self.claude_root.mkdir()
        self.inbox.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def env(self):
        value = os.environ.copy()
        value.update(
            {
                "AGENT_SESSION_MEMORY_DB": str(self.db),
                "AGENT_SESSION_MEMORY_CODEX_ROOT": str(self.codex_root),
                "AGENT_SESSION_MEMORY_CLAUDE_ROOT": str(self.claude_root),
                "AGENT_HOOK_CAPTURE": "0",
            }
        )
        return value

    def run_memory(self, *args):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return json.loads(result.stdout)

    def write_inbox_record(self, name, session_id, transcript_path=None, event="SessionEnd"):
        record = {
            "schema_version": 1,
            "status": "pending_review",
            "harness": "codex",
            "event": event,
            "record_kind": "checkpoint" if event == "PreCompact" else "session_end",
            "recorded_at": "2026-09-14T00:00:00+00:00",
            "session_id": session_id,
            "cwd": str(self.root),
        }
        if transcript_path is not None:
            record["transcript_path"] = str(transcript_path)
        path = self.inbox / name
        path.write_text(json.dumps(record) + "\n")
        return path

    def write_codex_trace(self):
        path = self.codex_root / "2026" / "session.jsonl"
        path.parent.mkdir()
        records = [
            {
                "type": "session_meta",
                "payload": {"session_id": "codex-test-session", "cwd": "/tmp/project"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "text", "text": "Choose the blue parser"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "do_not_index",
                    "input": "tool payload must not be stored",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "We chose the blue parser because it is deterministic",
                        }
                    ],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "api_key=supersecret12345"}],
                },
            },
        ]
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        return path

    def write_claude_trace(self):
        path = self.claude_root / "project" / "claude-session.jsonl"
        path.parent.mkdir()
        records = [
            {"type": "last-prompt", "sessionId": "claude-test-session"},
            {
                "type": "user",
                "sessionId": "claude-test-session",
                "message": {"role": "user", "content": "Review the policy decision"},
            },
            {
                "type": "assistant",
                "sessionId": "claude-test-session",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "ignored-tool", "input": {"secret": "no"}},
                        {"type": "text", "text": "The policy decision needs explicit provenance"},
                    ],
                },
            },
            {
                "type": "user",
                "sessionId": "claude-test-session",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "ignored result"}],
                },
            },
        ]
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        return path

    def wait_for_index(self, session_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.run_memory("get", session_id)
            if result["results"]:
                return result
            time.sleep(0.1)
        self.fail(f"index did not contain {session_id}")

    def test_cli_parses_both_harnesses_searches_and_is_idempotent(self):
        codex_path = self.write_codex_trace()
        claude_path = self.write_claude_trace()

        first = self.run_memory("index", "--harness", "all")
        self.assertEqual(first["processed"], 2)
        before = self.run_memory("status")
        self.assertEqual(before["indexed_sources"], 2)
        self.assertEqual(before["indexed_sessions"], 2)

        second = self.run_memory("index", "--harness", "all")
        self.assertEqual(second["processed"], 2)
        self.assertTrue(all(row["status"] == "unchanged" for row in second["results"]))
        after = self.run_memory("status")
        self.assertEqual(after["text_blocks"], before["text_blocks"])

        search = self.run_memory("search", "blue parser")
        self.assertTrue(any(row["session_id"] == "codex-test-session" for row in search["results"]))
        listed = self.run_memory("sessions")
        self.assertEqual(
            {row["session_id"] for row in listed["sessions"]},
            {"codex-test-session", "claude-test-session"},
        )
        codex = self.run_memory("get", "codex-test-session")
        codex_text = json.dumps(codex)
        self.assertIn("blue parser", codex_text)
        self.assertNotIn("supersecret12345", codex_text)
        self.assertNotIn("tool payload must not be stored", codex_text)

        claude = self.run_memory("get", "claude-test-session")
        claude_text = json.dumps(claude)
        self.assertIn("explicit provenance", claude_text)
        self.assertNotIn("ignored-tool", claude_text)
        self.assertNotIn("ignored result", claude_text)
        self.assertEqual(len(claude["results"]), 2)
        self.assertTrue(codex_path.exists())
        self.assertTrue(claude_path.exists())

    def test_governance_hook_records_and_indexes_without_blocking(self):
        trace_path = self.write_codex_trace()
        payload = {
            "session_id": "codex-test-session",
            "transcript_path": str(trace_path),
            "cwd": "/tmp/project",
            "reason": "e2e",
        }
        value = self.env()
        value.update(
            {
                "AGENT_HOOK_INBOX": str(self.inbox),
                "AGENT_HOOK_HARNESS": "codex",
                "AGENT_HOOK_EVENT": "SessionEnd",
            }
        )
        started = time.monotonic()
        subprocess.run(
            [sys.executable, str(GOVERNANCE)],
            input=json.dumps(payload),
            env=value,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        self.assertLess(time.monotonic() - started, 2.5)
        records = sorted(self.inbox.glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(record["status"], "pending_review")
        self.assertEqual(record["session_id"], "codex-test-session")
        self.wait_for_index("codex-test-session")

        value["AGENT_HOOK_EVENT"] = "SessionStart"
        result = subprocess.run(
            [sys.executable, str(GOVERNANCE)],
            input="{}",
            env=value,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        notice = json.loads(result.stdout)
        self.assertIn("Agent memory:", notice["systemMessage"])

    def test_precompact_hook_keeps_checkpoint_semantics_and_indexes(self):
        trace_path = self.write_codex_trace()
        payload = {
            "session_id": "codex-test-session",
            "transcript_path": str(trace_path),
            "cwd": "/tmp/project",
            "reason": "compaction",
        }
        value = self.env()
        value.update(
            {
                "AGENT_HOOK_INBOX": str(self.inbox),
                "AGENT_HOOK_HARNESS": "codex",
                "AGENT_HOOK_EVENT": "PreCompact",
            }
        )
        subprocess.run(
            [sys.executable, str(GOVERNANCE)],
            input=json.dumps(payload),
            env=value,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        records = sorted(self.inbox.glob("*.json"))
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].name.startswith("checkpoint-codex-codex-test-session"))
        record = json.loads(records[0].read_text())
        self.assertEqual(record["record_kind"], "checkpoint")
        self.wait_for_index("codex-test-session")

    def test_read_only_commands_do_not_create_or_change_the_index(self):
        missing_db = self.root / "missing.sqlite3"
        status = self.run_memory("--db", str(missing_db), "status")
        self.assertFalse(status["initialized"])
        self.assertFalse(missing_db.exists())

        self.assertEqual(self.run_memory("--db", str(missing_db), "search", "anything")["results"], [])
        self.assertFalse(missing_db.exists())

        self.write_codex_trace()
        self.run_memory("index", "--harness", "codex")
        before = self.db.stat()
        self.run_memory("status")
        self.run_memory("search", "blue parser")
        self.run_memory("get", "codex-test-session")
        self.run_memory("sessions")
        after = self.db.stat()
        self.assertEqual((after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns))

    def test_malformed_jsonl_is_retained_as_incomplete_evidence(self):
        path = self.codex_root / "malformed.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"session_id": "malformed-session"},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "text", "text": "preserve this evidence"}],
                    },
                }
            )
            + "\n{not valid json\n"
        )
        result = self.run_memory("index", "--harness", "codex", "--path", str(path))
        self.assertEqual(result["results"][0]["status"], "incomplete")
        self.assertEqual(result["results"][0]["malformed_lines"], 1)
        status = self.run_memory("status")
        self.assertEqual(status["incomplete_sources"], 1)
        evidence = self.run_memory("get", "malformed-session")
        self.assertIn("preserve this evidence", json.dumps(evidence))

    def test_failed_reindex_preserves_the_previous_index(self):
        path = self.write_codex_trace()
        self.run_memory("index", "--harness", "codex", "--path", str(path))
        connection = sqlite3.connect(self.db)
        connection.execute(
            """
            CREATE TRIGGER reject_reindex BEFORE INSERT ON blocks
            BEGIN SELECT RAISE(ABORT, 'synthetic reindex failure'); END;
            """
        )
        connection.commit()
        connection.close()
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"session_id": "codex-test-session"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "text", "text": "new block"}],
                            },
                        }
                    ),
                ]
            )
            + "\n"
        )
        result = self.run_memory("index", "--harness", "codex", "--path", str(path))
        self.assertEqual(result["results"][0]["status"], "error")
        previous = self.run_memory("get", "codex-test-session")
        self.assertIn("blue parser", json.dumps(previous))

    def test_repeated_text_blocks_get_distinct_ids(self):
        path = self.codex_root / "repeated-text.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"session_id": "repeated-text-session"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": "same block"},
                                    {"type": "text", "text": "same block"},
                                ],
                            },
                        }
                    ),
                ]
            )
            + "\n"
        )
        result = self.run_memory("index", "--harness", "codex", "--path", str(path))
        self.assertEqual(result["results"][0]["status"], "indexed")
        self.assertEqual(result["results"][0]["blocks"], 2)
        evidence = self.run_memory("get", "repeated-text-session")
        self.assertEqual(len(evidence["results"]), 2)

    def test_learning_review_is_stable_and_deduplicated_by_session(self):
        trace_path = self.write_codex_trace()
        self.write_inbox_record("a.json", "session-one", trace_path, "PreCompact")
        self.write_inbox_record("b.json", "session-one", trace_path, "SessionEnd")
        self.write_inbox_record("c.json", "session-two")
        self.write_inbox_record(
            "d.json", "session-three", self.root / "does-not-exist.jsonl"
        )

        first = self.run_memory("learning-review", "--inbox", str(self.inbox))
        second = self.run_memory("learning-review", "--inbox", str(self.inbox))
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["sessions"], second["sessions"])
        self.assertEqual(first["pending_records"], 4)
        self.assertEqual(first["pending_sessions"], 3)
        self.assertEqual(first["reviewable_records"], 2)
        self.assertEqual(first["reviewable_sessions"], 1)
        self.assertEqual(first["missing_transcript_path_records"], 1)
        self.assertEqual(first["unavailable_transcript_records"], 1)
        self.assertEqual(first["duplicate_sessions"], 1)
        self.assertEqual(first["sessions"][0]["record_count"], 2)

    def test_writable_index_migrates_the_previous_schema(self):
        connection = sqlite3.connect(self.db)
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE sources (
                source_key TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                harness TEXT NOT NULL,
                session_id TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                line_count INTEGER NOT NULL,
                text_blocks INTEGER NOT NULL,
                status TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                error TEXT
            );
            """
        )
        connection.commit()
        connection.close()

        path = self.write_codex_trace()
        result = self.run_memory("index", "--harness", "codex", "--path", str(path))
        self.assertEqual(result["results"][0]["status"], "indexed")

        connection = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sources)")}
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        connection.close()
        self.assertIn("content_sha256", columns)
        self.assertIn("malformed_lines", columns)
        self.assertEqual(version, "2")


if __name__ == "__main__":
    unittest.main()
