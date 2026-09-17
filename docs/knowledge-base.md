# OpenKnowledge

OpenKnowledge is the durable knowledge-base layer in this setup. It is useful
for reusable working agreements, verified explanations, operational context,
and decisions that should survive a single agent session.

See the [official OpenKnowledge overview](https://openknowledge.ai/docs/get-started/overview)
for the current desktop, CLI, MCP, and skills integration options. The
repository policy does not assume one installation method.

## Boundaries

- Treat the knowledge base as curated documentation, not live runtime,
  deployment, source, or data evidence.
- Preserve source, author, date, revision, and provenance where available.
- Keep local absolute paths and private identifiers out of portable documents.
- Review proposed additions before promotion.
- Prefer one canonical document over competing copies.

## Routing

- Repository-specific rules belong in that repository's `AGENTS.md`,
  `CLAUDE.md`, or equivalent policy surface.
- General agent behaviour belongs in global policy.
- Reusable cross-project context belongs in OpenKnowledge after review.
- Temporary task state belongs in a session handoff or local workspace.

## Hooks and lifecycle evidence

The hooks in this repository can record metadata-only pending lifecycle records
and capture marked review proposals. They do not write directly to
OpenKnowledge, infer durable lessons from a single session, or prune records
without an explicit decision and exact-path hash check.
