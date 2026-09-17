# Prerequisites and Integrations

Cockpit has two useful operating levels. The repository's hook implementations
and tests are self-contained. The full agent workflow additionally depends on
configured command-line tools and MCP servers.

## Minimum for the repository itself

| Requirement | Why | Check |
| --- | --- | --- |
| Git | Repository orientation and normal source control | `git --version` |
| Python 3.10+ | Hooks and tests | `python3 --version` |
| SQLite with FTS5 | Local session-memory index | See the check below |
| A hook-capable agent client | To receive lifecycle events | Check the client configuration |

The included tests do not require Repowise, Sentrux, Skillet, OpenKnowledge,
network access, credentials, or production data.

Check SQLite FTS5 without creating a file:

```bash
python3 - <<'PY'
import sqlite3

connection = sqlite3.connect(":memory:")
connection.execute("create virtual table cockpit_fts using fts5(text)")
print("SQLite FTS5: OK")
PY
```

## Full workflow integrations

These are capabilities, not hard-coded repository dependencies. Configure only
the integrations you intend to use, and keep their configuration in the agent
host rather than committing credentials or machine-specific paths here.

| Capability | Required when | Integration to configure | Safe verification |
| --- | --- | --- | --- |
| Repository understanding and change intelligence | You want indexed context, dependency/history queries, or impact analysis | Repowise CLI; optionally its MCP/agent integration | `repowise --version`; inspect index status for the target repository |
| Structural feedback and regression gating | You want architecture scans or before/after structural checks | Sentrux MCP server backed by the `sentrux` binary | `sentrux --version`; verify the client sees `scan` and related tools |
| Approved skill discovery | You want the Skillet-first routing policy to execute | An approved Skillet MCP server exposing skill search and selection | Verify the server connection, then run a read-only skill search |
| Durable knowledge base | You want agents to search or update OpenKnowledge | OpenKnowledge MCP/CLI integration | Verify a read-only search/read operation |
| Lifecycle capture and local evidence review | You want session-start, compaction, session-end, and review hooks | Copy the selected hooks and merge the configuration fragments | Run the isolated tests and a temporary-directory smoke test |

### Project links

- [Repowise documentation](https://docs.repowise.dev/) and
  [source repository](https://github.com/repowise-dev/repowise)
- [Sentrux installation](https://sentrux.dev/docs/installation/),
  [MCP integration](https://sentrux.dev/docs/mcp/), and
  [source repository](https://github.com/sentrux/sentrux)
- [OpenKnowledge overview](https://openknowledge.ai/docs/get-started/overview)
- [Skillet source used by this setup](https://github.com/mhingston/skillet)

The Skillet source and deployment are intentionally replaceable. If another
approved Skillet service is used, update the host MCP configuration without
changing the repository policy.

## MCP configuration boundary

The server names below are conventions used by the current setup, not
repository requirements:

```text
skillet         skill discovery and selection
sentrux         structural scans and regression feedback
open-knowledge  knowledge-base search and maintenance
```

MCP configuration is client-specific. The examples in this repository show
hook configuration, not a universal MCP installer. Configure each server in
Codex, Claude, or another MCP client according to that client's current
syntax, then verify the connection and exposed tools from the client.

For Sentrux, the command-line MCP entrypoint can vary by release. Follow the
upstream integration guide and verify the installed binary instead of copying
an old command verbatim. For Skillet, the current setup uses a loopback MCP
service; a reusable installation should treat the endpoint as a replaceable
host setting.

## Repowise index safety

Repowise's CLI and agent integration are useful only when the target index is
fresh. Before relying on a repository-local index, compare its recorded source
revision with the checkout's current Git revision.

Initialising or refreshing an index writes repository state. Before running
those commands, verify that `.repowise/` is ignored and obtain the approval
required by the target repository's policy. Do not make indexing a hidden side
effect of ordinary inspection.

## Readiness checklist

```text
[ ] Base checks pass: Git, Python, SQLite FTS5
[ ] The selected agent client loads the hook configuration
[ ] Sentrux MCP is connected, if structural checks are in scope
[ ] Skillet MCP is connected, if specialised routing is in scope
[ ] OpenKnowledge is connected, if durable knowledge is in scope
[ ] Repowise is installed and its target index is fresh, if repository
    intelligence is in scope
[ ] `python3 -m unittest discover -s tests -v` passes
[ ] No transcripts, credentials, runtime databases, or host paths are tracked
```
