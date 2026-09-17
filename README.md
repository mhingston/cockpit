# Cockpit

Cockpit is a portable template for a personal agentic development setup. It
documents the operating rules, evidence boundaries, repository-intelligence
tools, skill discovery, and lifecycle hooks that help an agent work safely and
repeatably across unrelated repositories.

The template is deliberately organisation-neutral. It contains no customer,
employer, product, repository, account, transcript, credential, or machine
state.

## Contents

- [`AGENTS.md`](AGENTS.md): compact policy loaded by repository-aware agents.
- [`WORKFLOW_USAGE.md`](WORKFLOW_USAGE.md): light, medium, and heavy execution
  routes.
- [`docs/knowledge-base.md`](docs/knowledge-base.md): using OpenKnowledge as a
  governed durable knowledge base.
- [`docs/repository-intelligence.md`](docs/repository-intelligence.md): the
  complementary roles of Repowise and Sentrux.
- [`docs/skills.md`](docs/skills.md): Skillet-first skill discovery and
  materialisation boundaries.
- [`docs/hooks.md`](docs/hooks.md): lifecycle capture, local evidence indexing,
  review proposals, and configuration guidance.
- [`hooks/`](hooks/): portable, opt-in Python hook implementations.
- [`examples/`](examples/): minimal Codex and Claude configuration fragments.
- [`tests/`](tests/): isolated tests using temporary directories only.

## Design principles

1. Evidence outranks inference. Source, build, pull request, deployment,
   runtime, data, and review evidence are distinct claims.
2. Read-only inspection is the default. Mutations require a bounded target,
   an explicit request, and post-change verification.
3. Derived indexes and scores are navigation or feedback aids, not canonical
   truth and not permission to change source.
4. Lifecycle automation may capture metadata and create review proposals. It
   does not promote durable knowledge or delete records automatically.
5. Local transcripts and derived indexes stay local. This repository contains
   code and policy, not captured sessions.

## Verify the template

From the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q hooks
python3 -m json.tool examples/codex-hooks.json >/dev/null
python3 -m json.tool examples/claude-settings.fragment.json >/dev/null
```

The tests create temporary fixture directories and do not read or write the
host agent configuration.

## Adapting it

1. Copy or selectively adapt `AGENTS.md` to the scope where it should apply.
2. Read the documentation and choose only the hooks and tools that fit the
   host.
3. Set the environment variables described in `docs/hooks.md` rather than
   hard-coding home-directory paths.
4. Integrate the configuration fragments manually with the existing Codex or
   Claude configuration; preserve unrelated entries.
5. Keep inboxes, outboxes, transcripts, SQLite databases, caches, credentials,
   and generated files outside the repository.

The repository is a starting point, not an installer. Host configuration
changes should be reviewed and tested separately from changes to this template.
