# Lifecycle Hooks

The hook layer is intentionally fail-open: an indexing, review, or notification
failure must not block the agent. A hook should be fast, bounded, and explicit
about what it stores.

## Current pattern

```text
agent event
  -> metadata-only lifecycle receipt in a local inbox
  -> optional detached local text index with redaction
  -> session-start status notice
  -> marked review proposal in a local outbox
  -> manual decision and separately approved archival
```

The implementation supports Codex- and Claude-shaped JSONL records, but the
paths and names are configurable.

## Included components

- `agent_governance.py`: handles `SessionStart`, `PreCompact`, and
  `SessionEnd`; records metadata, reports pending review counts, and starts
  advisory background work.
- `session_memory.py`: builds a local SQLite/FTS5 index of redacted user and
  assistant text only. Tool inputs, tool outputs, reasoning blocks, and
  credentials are excluded.
- `learning_review.py`: captures a final assistant report only when it ends in
  the configured marker, records it as pending approval, and provides explicit
  decision plus hash-checked exact-path archive and prune commands.
- `../skills/learning-review-approval/SKILL.md`: an optional, generic
  human-gated second stage for evidence review, durable decisions, and
  separately approved archival.
- `session_start_orientation.py`: injects read-only Git branch and status
  context.
- `optional/dotnet_nodereuse_guard.py`: an optional example of a narrow
  command guard for environments with a known build-process failure mode.

## Data boundary

The repository must never receive live inbox files, outbox artifacts,
transcripts, SQLite databases, caches, or credentials. Keep those under a
user-local directory with restrictive permissions. The derived index is a
navigation aid, not canonical transcript evidence.

Lifecycle capture is not promotion. Missing, unavailable, malformed, or
incomplete transcript evidence remains unresolved. Multiple records for one
session are deduplicated during review rather than silently merged or deleted.

## Configuration

The default paths are under `~/.agent-hooks` for compatibility with the
existing pattern. Override them for another host:

```text
AGENT_HOOK_INBOX
AGENT_HOOK_OUTBOX
AGENT_HOOK_CAPTURE_OUTBOX
AGENT_HOOK_ARCHIVE
AGENT_HOOK_HARNESS
AGENT_HOOK_EVENT
AGENT_SESSION_MEMORY_DB
AGENT_SESSION_MEMORY_CODEX_ROOT
AGENT_SESSION_MEMORY_CLAUDE_ROOT
AGENT_LEARNING_MARKER
```

Copy the scripts to a user-local hooks directory, make entrypoints executable,
and adapt the configuration fragments in `examples/`. Merge them with the
existing host configuration instead of replacing it.

If lifecycle-learning review is enabled, also expose the bundled approval skill
to the host skill directory and configure the review adapter and durable
destinations. The skill is portable, but its paths, knowledge-base integration,
and destination policy remain host-specific.

## Verification

Test the scripts with temporary paths before enabling them. Confirm that:

- malformed input fails open;
- lifecycle receipts contain metadata but no transcript content;
- indexing redacts credential-shaped values and ignores tool records;
- read-only index commands do not create or modify the database;
- capture is marker-gated and idempotent;
- archive requires exact paths, original hashes, a confirmation digest, and
  complete preflight before moving any record;
- prune requires exact paths, original hashes, and a confirmation digest; and
- hook subprocesses finish within their configured timeout.
