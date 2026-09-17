#!/usr/bin/env python3
"""Local, deterministic evidence index for Codex and Claude sessions.

This is a derived, local-only index.  It stores only textual user/assistant
message blocks, with common credential-shaped values redacted before storage.
Tool inputs, tool outputs, reasoning blocks, prompts, and network publishing
are intentionally out of scope.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path


SCHEMA_VERSION = 2
DEFAULT_MAX_CHARS = 1800
DEFAULT_OVERLAP = 180
HARNESS_NAMES = ("codex", "claude")
TEXT_BLOCK_TYPES = {"text", "input_text", "output_text"}

SECRET_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(authorization\s*:\s*)[^\s]+"), r"\1[REDACTED]"),
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|"
            r"AIza[0-9A-Za-z_-]{20,})\b"
        ),
        "[REDACTED]",
    ),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
        "[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)(\b(?:password|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*)"
            r"[\"']?[^\s,\"']{8,}"
        ),
        r"\1[REDACTED]",
    ),
)


def home_path(name: str) -> Path:
    return Path.home() / name


def memory_home() -> Path:
    configured = os.environ.get("AGENT_SESSION_MEMORY_HOME")
    if configured:
        return Path(configured).expanduser()
    return home_path(".agent-hooks/session-memory")


def database_path() -> Path:
    configured = os.environ.get("AGENT_SESSION_MEMORY_DB")
    if configured:
        return Path(configured).expanduser()
    return memory_home() / "memory.sqlite3"


def source_roots(harness: str) -> list[tuple[str, Path]]:
    roots = {
        "codex": Path(
            os.environ.get(
                "AGENT_SESSION_MEMORY_CODEX_ROOT", str(home_path(".codex/sessions"))
            )
        )
        .expanduser()
        .resolve(),
        "claude": Path(
            os.environ.get(
                "AGENT_SESSION_MEMORY_CLAUDE_ROOT", str(home_path(".claude/projects"))
            )
        )
        .expanduser()
        .resolve(),
    }
    names = HARNESS_NAMES if harness == "all" else (harness,)
    return [(name, roots[name]) for name in names]


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("\x00", " ")
    for pattern, replacement in SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value.strip()


def text_blocks(value: object) -> list[str]:
    if isinstance(value, str):
        text = safe_text(value)
        return [text] if text else []
    if not isinstance(value, list):
        return []
    blocks = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") not in TEXT_BLOCK_TYPES:
            continue
        text = safe_text(item.get("text"))
        if text:
            blocks.append(text)
    return blocks


def split_text(text: str, max_chars: int, overlap: int) -> list[tuple[int, int, str]]:
    if not text:
        return []
    if max_chars <= 0 or overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be >= 0 and smaller than max_chars")
    if len(text) <= max_chars:
        return [(0, len(text), text)]
    step = max_chars - overlap
    result = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        result.append((start, end, text[start:end]))
        if end == len(text):
            break
        start += step
    return result


def path_allowed(path: Path, harness: str) -> bool:
    resolved = path.expanduser().resolve()
    for _, root in source_roots(harness):
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def is_subagent(path: Path) -> bool:
    return "subagents" in path.parts


def configure_connection(path: Path) -> sqlite3.Connection:
    """Open the writable index and apply the small, local schema migration."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources (
            source_key TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            harness TEXT NOT NULL,
            session_id TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            line_count INTEGER NOT NULL,
            text_blocks INTEGER NOT NULL,
            content_sha256 TEXT,
            malformed_lines INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS blocks (
            block_id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL,
            harness TEXT NOT NULL,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            line_start INTEGER NOT NULL,
            line_end INTEGER NOT NULL,
            part INTEGER NOT NULL,
            text_hash TEXT NOT NULL,
            text TEXT NOT NULL,
            FOREIGN KEY (source_key) REFERENCES sources(source_key) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS blocks_session_idx ON blocks(session_id, line_start);
        CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
            block_id UNINDEXED,
            harness UNINDEXED,
            session_id UNINDEXED,
            role UNINDEXED,
            source_path UNINDEXED,
            line_start UNINDEXED,
            line_end UNINDEXED,
            text
        );
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(sources)").fetchall()
    }
    if "content_sha256" not in columns:
        connection.execute("ALTER TABLE sources ADD COLUMN content_sha256 TEXT")
    if "malformed_lines" not in columns:
        connection.execute(
            "ALTER TABLE sources ADD COLUMN malformed_lines INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()
    return connection


def readonly_connection(path: Path) -> sqlite3.Connection | None:
    """Open an existing index without creating, migrating, or journaling it."""
    if not path.is_file():
        return None
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1)
    connection.execute("PRAGMA busy_timeout=1000")
    return connection


@contextlib.contextmanager
def writer_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def json_lines(path: Path):
    """Read JSONL while retaining line counts and malformed-line evidence."""
    values = []
    malformed_lines = 0
    line_count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw in enumerate(handle, 1):
            line_count = line_number
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue
            if isinstance(value, dict):
                values.append((line_number, value))
            else:
                malformed_lines += 1
    return values, line_count, malformed_lines


def codex_records(path: Path):
    session_id = ""
    records = []
    values, line_count, malformed_lines = json_lines(path)
    for line_number, value in values:
        payload = value.get("payload")
        if value.get("type") == "session_meta" and isinstance(payload, dict):
            session_id = str(payload.get("session_id") or payload.get("id") or session_id)
        if value.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
            continue
        for text in text_blocks(payload.get("content")):
            records.append((session_id, payload["role"], line_number, line_number, text))
    return session_id or path.stem, records, line_count, malformed_lines


def claude_records(path: Path):
    session_id = ""
    records = []
    values, line_count, malformed_lines = json_lines(path)
    for line_number, value in values:
        candidate = value.get("sessionId")
        if isinstance(candidate, str) and candidate:
            session_id = candidate
        if value.get("type") not in {"user", "assistant"}:
            continue
        message = value.get("message")
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        for text in text_blocks(message.get("content")):
            records.append((session_id, message["role"], line_number, line_number, text))
    return session_id or path.parent.name or path.stem, records, line_count, malformed_lines


def parse_source(path: Path, harness: str):
    if harness == "codex":
        return codex_records(path)
    return claude_records(path)


def source_key(path: Path, harness: str) -> str:
    return hashlib.sha256(f"{harness}\0{path.resolve()}".encode()).hexdigest()


def block_id(
    harness: str,
    session_id: str,
    line_start: int,
    role: str,
    record_index: int,
    part: int,
    text: str,
) -> str:
    value = (
        f"{harness}\0{session_id}\0{line_start}\0{role}\0"
        f"{record_index}\0{part}\0{text}"
    )
    return hashlib.sha256(value.encode()).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def index_one(connection: sqlite3.Connection, path: Path, harness: str, max_chars: int, overlap: int):
    path = path.expanduser().resolve()
    source_id = source_key(path, harness)
    try:
        stat = path.stat()
        existing = connection.execute(
            "SELECT size, mtime_ns, status FROM sources WHERE source_key = ?", (source_id,)
        ).fetchone()
        if existing and existing == (stat.st_size, stat.st_mtime_ns, "complete"):
            return {"path": str(path), "status": "unchanged", "blocks": 0}
        session_id, records, line_count, malformed_lines = parse_source(path, harness)
        content_sha256 = file_sha256(path)
        block_rows = []
        for record_index, (session_value, role, line_start, line_end, text) in enumerate(records):
            session_value = session_value or session_id
            for part, (_, _, chunk) in enumerate(split_text(text, max_chars, overlap)):
                digest = hashlib.sha256(chunk.encode()).hexdigest()
                identifier = block_id(
                    harness,
                    session_value,
                    line_start,
                    role,
                    record_index,
                    part,
                    chunk,
                )
                block_rows.append(
                    (
                        identifier,
                        source_id,
                        harness,
                        session_value,
                        role,
                        line_start,
                        line_end,
                        part,
                        digest,
                        chunk,
                    )
                )
        connection.execute("SAVEPOINT reindex_source")
        try:
            connection.execute(
                "DELETE FROM blocks_fts WHERE block_id IN "
                "(SELECT block_id FROM blocks WHERE source_key = ?)",
                (source_id,),
            )
            connection.execute("DELETE FROM blocks WHERE source_key = ?", (source_id,))
            source_status = "incomplete" if malformed_lines else "complete"
            connection.execute(
                """
                INSERT INTO sources(source_key, path, harness, session_id, size, mtime_ns,
                                    line_count, text_blocks, content_sha256, malformed_lines,
                                    status, indexed_at, error)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(source_key) DO UPDATE SET
                    path=excluded.path, harness=excluded.harness, session_id=excluded.session_id,
                    size=excluded.size, mtime_ns=excluded.mtime_ns, line_count=excluded.line_count,
                    text_blocks=excluded.text_blocks, content_sha256=excluded.content_sha256,
                    malformed_lines=excluded.malformed_lines, status=excluded.status,
                    indexed_at=excluded.indexed_at, error=NULL
                """,
                (
                    source_id,
                    str(path),
                    harness,
                    session_id,
                    stat.st_size,
                    stat.st_mtime_ns,
                    line_count,
                    len(block_rows),
                    content_sha256,
                    malformed_lines,
                    source_status,
                    iso_now(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO blocks(block_id, source_key, harness, session_id, role, line_start,
                                   line_end, part, text_hash, text)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                block_rows,
            )
            connection.executemany(
                """
                INSERT INTO blocks_fts(block_id, harness, session_id, role, source_path,
                                       line_start, line_end, text)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (row[0], row[2], row[3], row[4], str(path), row[5], row[6], row[9])
                    for row in block_rows
                ],
            )
            connection.execute("RELEASE SAVEPOINT reindex_source")
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT reindex_source")
            connection.execute("RELEASE SAVEPOINT reindex_source")
            raise
        return {
            "path": str(path),
            "status": "incomplete" if malformed_lines else "indexed",
            "blocks": len(block_rows),
            "malformed_lines": malformed_lines,
        }
    except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
        connection.execute(
            """
            INSERT INTO sources(source_key, path, harness, session_id, size, mtime_ns,
                                line_count, text_blocks, status, indexed_at, error)
            VALUES(?, ?, ?, ?, 0, 0, 0, 0, 'error', ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET status='error', indexed_at=excluded.indexed_at,
                error=excluded.error
            """,
            (source_id, str(path), harness, path.stem, iso_now(), str(exc)[:300]),
        )
        return {"path": str(path), "status": "error", "blocks": 0, "error": str(exc)[:300]}


def discover_sources(harness: str, explicit_paths: list[str], session_id: str, limit: int):
    selected = []
    for raw in explicit_paths:
        path = Path(raw).expanduser().resolve()
        for name, _ in source_roots(harness):
            if path_allowed(path, name) and path.suffix == ".jsonl" and not is_subagent(path):
                selected.append((name, path))
                break
    if not explicit_paths:
        for name, root in source_roots(harness):
            if not root.is_dir():
                continue
            paths = sorted(
                (path for path in root.rglob("*.jsonl") if not is_subagent(path)),
                key=lambda value: value.stat().st_mtime_ns if value.exists() else 0,
                reverse=True,
            )
            selected.extend((name, path) for path in paths)
    result = []
    seen = set()
    for name, path in selected:
        key = (name, str(path))
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        if session_id:
            try:
                parsed_session, _, _, _ = parse_source(path, name)
            except (OSError, UnicodeError):
                continue
            if parsed_session != session_id:
                continue
        result.append((name, path))
        if limit and len(result) >= limit:
            break
    return result


def run_index(args) -> dict:
    db_path = database_path()
    selected = discover_sources(args.harness, args.path, args.session_id, args.limit)
    started = time.monotonic()
    results = []
    with writer_lock(db_path):
        connection = configure_connection(db_path)
        try:
            for harness, path in selected:
                if args.max_seconds and time.monotonic() - started >= args.max_seconds:
                    break
                results.append(index_one(connection, path, harness, args.max_chars, args.overlap))
                connection.commit()
                if args.max_seconds and time.monotonic() - started >= args.max_seconds:
                    break
        finally:
            connection.close()
    return {
        "database": str(db_path),
        "selected": len(selected),
        "processed": len(results),
        "results": results,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def status() -> dict:
    db_path = database_path()
    if not db_path.is_file():
        return {
            "database": str(db_path),
            "initialized": False,
            "indexed_sources": 0,
            "indexed_sessions": 0,
            "text_blocks": 0,
            "errors": 0,
            "incomplete_sources": 0,
            "last_indexed_at": None,
        }
    try:
        connection = readonly_connection(db_path)
    except sqlite3.Error as exc:
        return {
            "database": str(db_path),
            "initialized": True,
            "indexed_sources": 0,
            "indexed_sessions": 0,
            "text_blocks": 0,
            "errors": 0,
            "incomplete_sources": 0,
            "last_indexed_at": None,
            "read_error": str(exc)[:300],
        }
    try:
        sources, sessions, blocks, errors, incomplete, last_indexed = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM sources WHERE status='complete'),
                (SELECT count(DISTINCT session_id) FROM sources WHERE status='complete'),
                (SELECT count(*) FROM blocks),
                (SELECT count(*) FROM sources WHERE status='error'),
                (SELECT count(*) FROM sources WHERE status='incomplete'),
                (SELECT max(indexed_at) FROM sources WHERE status='complete')
            """
        ).fetchone()
    finally:
        connection.close()
    return {
        "database": str(db_path),
        "initialized": True,
        "indexed_sources": sources,
        "indexed_sessions": sessions,
        "text_blocks": blocks,
        "errors": errors,
        "incomplete_sources": incomplete,
        "last_indexed_at": last_indexed,
    }


def hook_status() -> str:
    value = status()
    if not value["initialized"]:
        return "Agent memory: local index not initialized; lifecycle indexing is enabled."
    if value.get("read_error"):
        return "Agent memory: local index status unavailable; local-only derived index."
    return (
        "Agent memory: "
        f"{value['indexed_sessions']} indexed session(s), "
        f"{value['text_blocks']} text block(s), "
        f"{value['errors']} source error(s), "
        f"{value['incomplete_sources']} incomplete source(s); local-only derived index."
    )


def transcript_fingerprint(record: dict) -> dict:
    """Classify a referenced transcript without reading or storing its content."""
    raw_path = record.get("transcript_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return {"state": "missing_path"}
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        cwd = record.get("cwd")
        candidate = Path(cwd).expanduser() / candidate if isinstance(cwd, str) and cwd else candidate
    try:
        resolved = candidate.resolve()
        stat = resolved.stat()
    except OSError:
        return {"state": "unavailable", "path": str(candidate)}
    if not resolved.is_file():
        return {"state": "not_file", "path": str(resolved)}
    return {
        "state": "available",
        "path": str(resolved),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def learning_review(inbox_path: str) -> dict:
    """Build a deterministic, non-mutating review manifest grouped by session."""
    inbox = Path(inbox_path).expanduser().resolve()
    grouped: dict[str, list[dict]] = {}
    malformed_records = []
    try:
        candidates = sorted(inbox.glob("*.json"))
    except OSError:
        candidates = []
        malformed_records.append({"path": str(inbox), "error": "inbox_unreadable"})

    for path in candidates:
        try:
            raw = path.read_bytes()
            record = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            malformed_records.append({"path": str(path), "error": type(exc).__name__})
            continue
        if not isinstance(record, dict):
            malformed_records.append({"path": str(path), "error": "not_object"})
            continue
        if record.get("status") != "pending_review":
            continue

        session_id = record.get("session_id")
        session_key = (
            session_id
            if isinstance(session_id, str) and session_id
            else f"record:{path.name}"
        )
        grouped.setdefault(session_key, []).append(
            {
                "record_path": str(path),
                "record_sha256": hashlib.sha256(raw).hexdigest(),
                "harness": record.get("harness"),
                "event": record.get("event"),
                "record_kind": record.get("record_kind"),
                "recorded_at": record.get("recorded_at"),
                "transcript": transcript_fingerprint(record),
            }
        )

    sessions = []
    for session_id in sorted(grouped):
        records = sorted(
            grouped[session_id],
            key=lambda value: (
                value.get("recorded_at") or "",
                value.get("event") or "",
                value["record_path"],
            ),
        )
        states = {value["transcript"]["state"] for value in records}
        sessions.append(
            {
                "session_id": session_id,
                "review_status": "reviewable" if "available" in states else "unresolved",
                "record_count": len(records),
                "duplicate": len(records) > 1,
                "events": sorted(
                    {
                        value.get("event")
                        for value in records
                        if isinstance(value.get("event"), str) and value.get("event")
                    }
                ),
                "records": records,
            }
        )

    pending_records = [record for session in sessions for record in session["records"]]
    state_counts = {
        state: sum(record["transcript"]["state"] == state for record in pending_records)
        for state in ("available", "missing_path", "unavailable", "not_file")
    }
    manifest = {
        "manifest_version": 1,
        "inbox": str(inbox),
        "pending_records": len(pending_records),
        "pending_sessions": len(sessions),
        "reviewable_records": state_counts["available"],
        "reviewable_sessions": sum(session["review_status"] == "reviewable" for session in sessions),
        "missing_transcript_path_records": state_counts["missing_path"],
        "unavailable_transcript_records": state_counts["unavailable"],
        "invalid_transcript_records": state_counts["not_file"],
        "duplicate_sessions": sum(session["duplicate"] for session in sessions),
        "malformed_records": malformed_records,
        "sessions": sessions,
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["snapshot_id"] = hashlib.sha256(canonical.encode()).hexdigest()
    return manifest


def fts_query(value: str) -> str:
    tokens = re.findall(r"[\w-]{2,}", value, flags=re.UNICODE)
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def search(value: str, limit: int) -> list[dict]:
    query = fts_query(value)
    if not query:
        return []
    connection = readonly_connection(database_path())
    if connection is None:
        return []
    try:
        rows = connection.execute(
            """
            SELECT block_id, harness, session_id, role, source_path, line_start, line_end,
                   snippet(blocks_fts, 7, '[', ']', ' … ', 32) AS excerpt
            FROM blocks_fts
            WHERE blocks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "block_id": row[0],
            "harness": row[1],
            "session_id": row[2],
            "role": row[3],
            "source_path": row[4],
            "line_start": row[5],
            "line_end": row[6],
            "excerpt": row[7],
        }
        for row in rows
    ]


def get_session(session_id: str, limit: int) -> list[dict]:
    connection = readonly_connection(database_path())
    if connection is None:
        return []
    try:
        rows = connection.execute(
            """
            SELECT block_id, harness, session_id, role, source_path, line_start, line_end, text
            FROM blocks_fts
            WHERE session_id = ?
            ORDER BY line_start, block_id
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "block_id": row[0],
            "harness": row[1],
            "session_id": row[2],
            "role": row[3],
            "source_path": row[4],
            "line_start": row[5],
            "line_end": row[6],
            "text": row[7],
        }
        for row in rows
    ]


def sessions(limit: int) -> list[dict]:
    connection = readonly_connection(database_path())
    if connection is None:
        return []
    try:
        rows = connection.execute(
            """
            SELECT harness, session_id, count(*) AS blocks, min(line_start), max(line_end),
                   min(source_path)
            FROM blocks_fts
            GROUP BY harness, session_id
            ORDER BY max(rowid) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "harness": row[0],
            "session_id": row[1],
            "text_blocks": row[2],
            "first_line": row[3],
            "last_line": row[4],
            "source_path": row[5],
        }
        for row in rows
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", dest="db", help="override the SQLite database path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index")
    index_parser.add_argument("--harness", choices=(*HARNESS_NAMES, "all"), default="all")
    index_parser.add_argument("--path", action="append", default=[])
    index_parser.add_argument("--session-id", default="")
    index_parser.add_argument("--limit", type=int, default=0)
    index_parser.add_argument("--max-seconds", type=float, default=0)
    index_parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    index_parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--format", choices=("json", "hook"), default="json")

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=8)

    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("session_id")
    get_parser.add_argument("--limit", type=int, default=100)

    sessions_parser = subparsers.add_parser("sessions")
    sessions_parser.add_argument("--limit", type=int, default=50)

    review_parser = subparsers.add_parser(
        "learning-review",
        help="emit a deterministic, deduplicated pending-review manifest",
    )
    review_parser.add_argument(
        "--inbox",
        default=os.environ.get(
            "AGENT_HOOK_INBOX", str(home_path(".agent-hooks/inbox"))
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.db:
        os.environ["AGENT_SESSION_MEMORY_DB"] = args.db
    if args.command == "index":
        print(json.dumps(run_index(args), indent=2, sort_keys=True))
    elif args.command == "status":
        print(hook_status() if args.format == "hook" else json.dumps(status(), indent=2, sort_keys=True))
    elif args.command == "search":
        print(json.dumps({"query": args.query, "results": search(args.query, args.limit)}, indent=2))
    elif args.command == "get":
        print(json.dumps({"session_id": args.session_id, "results": get_session(args.session_id, args.limit)}, indent=2))
    elif args.command == "sessions":
        print(json.dumps({"sessions": sessions(args.limit)}, indent=2))
    elif args.command == "learning-review":
        print(json.dumps(learning_review(args.inbox), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"agent-session-memory: {exc}", file=sys.stderr)
        raise SystemExit(1)
