# okf-mcp

**MCP server for [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog) bundles** — expose [okf-ingest](https://github.com/travisjakel/okf-ingest)'s deterministic consume verbs as tools any MCP client (Claude Code, Cursor, custom agents) can call.

Existing OKF MCP servers hand agents the markdown *files*. This one hands them
the **queryable catalog and concept graph**: index-first context assembly,
impact/backlink analysis, SQL over frontmatter, drift diffs, and health checks —
all deterministic okf-ingest code, no model calls in the server.

## Tools

| Tool | What the agent gets |
|---|---|
| `okf_list_bundles` | which bundles this server exposes |
| `okf_search` | concepts whose title/body match a term |
| `okf_get_concept` | one concept's frontmatter + full body |
| `okf_context` | **the flagship** — index.md + a concept's link-neighborhood as one curated markdown blob (the author's graph, not fuzzy matches) |
| `okf_impact` | inbound / outbound / transitive links — "what breaks if X changes" |
| `okf_sql` | read-only SELECT over the DuckDB catalog (`okf_concept`, `okf_link`, `okf_validation`) |
| `okf_diff` | what changed on disk since the server loaded the bundle — an agent's memory-refresh between looks |
| `okf_refresh` | re-ingest a directory bundle after `okf_diff` shows changes |
| `okf_doctor` | health score + per-rule findings before trusting a bundle |

## Install & run

```bash
pip install okf-mcp        # depends on okf-ingest >= 0.7.0

# one or more bundles: directories and/or okf-ingest .duckdb catalogs,
# optionally named (first one is the default target)
okf-mcp ~/my-bundle
okf-mcp wiki=~/wiki notes=~/vault snapshot=./kb.duckdb
```

Register with Claude Code:

```bash
claude mcp add okf -- okf-mcp ~/my-bundle
```

Or in any MCP client config:

```json
{ "mcpServers": { "okf": { "command": "okf-mcp", "args": ["wiki=/path/to/bundle"] } } }
```

## Design notes

- **Read-only.** Directory bundles are ingested into disposable in-memory
  DuckDB catalogs at startup; `.duckdb` sources open read-only; `okf_sql`
  accepts SELECT/WITH only. The server never writes to your bundle.
- **Deterministic.** Every tool is plain okf-ingest code — same input, same
  answer; the only nondeterminism an agent sees is its own.
- **Concepts resolve by name.** `okf_get_concept` / `okf_context` / `okf_impact`
  accept a path *or* a wikilink-style name — id, alias, title, or filename stem,
  compared on alphanumerics ('agent memory architecture' finds
  `ops/agent_memory_architecture.md`). Ambiguous names return the candidate
  list instead of guessing.
- **`[[wikilinks]]` work.** Vault-style bundles (Obsidian/Logseq/Foam) resolve
  by id / alias / title / stem, same as okf-ingest 0.6.0+.

Apache-2.0. Sibling project: [okf-ingest](https://github.com/travisjakel/okf-ingest) (the R + Python ingestion tool this wraps).
