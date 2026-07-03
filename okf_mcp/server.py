"""okf-mcp — MCP server exposing okf-ingest's consume verbs as agent tools.

Usage:
    okf-mcp <bundle> [<bundle> ...]

Each <bundle> is a directory (an OKF bundle of markdown files) or a .duckdb
catalog produced by `okf ingest`, optionally prefixed with a name:
    okf-mcp ~/wiki notes=~/vault snapshot=./kb.duckdb

Runs on stdio (the standard MCP local-server transport). Directory bundles are
ingested into disposable in-memory catalogs at startup; sources are never
written to. Register with an MCP client, e.g.:
    claude mcp add okf -- okf-mcp ~/my-bundle
"""
from __future__ import annotations

import datetime
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import registry as R

mcp = FastMCP("okf")
reg = R.BundleRegistry()


@mcp.tool()
def okf_list_bundles() -> list[dict]:
    """List the knowledge bundles this server exposes (name, source, concept
    count). Call this first when unsure which bundle to target; every other
    tool takes an optional `bundle` name and defaults to the first bundle."""
    return R.list_bundles(reg)


@mcp.tool()
def okf_search(term: str, bundle: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Find concepts whose title or body contains `term` (case-insensitive
    substring). Call this to locate relevant concepts before reading them —
    returns path/type/title/description; follow up with okf_get_concept or
    okf_context on a returned path."""
    return R.search(reg, term, bundle, limit)


@mcp.tool()
def okf_get_concept(path: str, bundle: Optional[str] = None) -> dict:
    """Read one concept (its frontmatter fields and full markdown body). Accepts
    a bundle-relative path ('ops/backups.md') OR a name — resolved like a
    [[wikilink]] by id, alias, title, or filename stem ('agent memory
    architecture' works). Ambiguous names return the candidates."""
    return R.get_concept(reg, path, bundle)


@mcp.tool()
def okf_context(start: Optional[str] = None, depth: int = 1,
                max_tokens: int = 8000, bundle: Optional[str] = None,
                rank: str = "ppr") -> dict:
    """Assemble a curated, index-first context blob: index.md plus the concept
    at `start` (a path or a wikilink-style name — id/alias/title/stem all
    resolve) and its most relevant neighborhood, as one markdown string
    (capped near `max_tokens`). Selection is ranked by exact Personalized
    PageRank over the author's link graph by default — the pages that matter
    most to the topic fill the budget first; pass rank='bfs' for a plain
    depth-limited neighborhood. Prefer this over search when you want the
    full picture of a topic. Omit `start` to pack the whole bundle."""
    return R.context(reg, start, depth, max_tokens, bundle, rank)


@mcp.tool()
def okf_related(concept: str, k: int = 10, bundle: Optional[str] = None) -> list[dict]:
    """The k concepts most relevant to `concept` by link structure (exact
    Personalized PageRank — deterministic, no embeddings, seed excluded).
    Call to discover what else matters about a topic when keyword search
    isn't enough; `concept` accepts a path or a wikilink-style name."""
    return R.related(reg, concept, k, bundle)


@mcp.tool()
def okf_impact(concept: str, bundle: Optional[str] = None) -> dict:
    """Report what links to and from a concept: outbound links, inbound links
    (backlinks), and the full transitive set of concepts reachable from it.
    Call this to answer 'what depends on X' or 'what breaks if X changes'.
    `concept` accepts a path or a wikilink-style name (id/alias/title/stem)."""
    return R.impact(reg, concept, bundle)


@mcp.tool()
def okf_sql(query: str, bundle: Optional[str] = None) -> list[dict]:
    """Run a read-only SQL (SELECT/WITH) query against the bundle's DuckDB
    catalog. Tables: okf_concept (path, type, title, description, tags,
    timestamp, body, content_hash), okf_link (src_path, dst_raw, dst_path,
    resolved), okf_validation (path, severity, rule, message). Use for
    structured questions the other tools don't cover, e.g. counting concepts
    by type or listing everything tagged 'x'."""
    return R.sql(reg, query, bundle)


@mcp.tool()
def okf_diff(bundle: Optional[str] = None) -> dict:
    """Report what changed in a directory-backed bundle since this server
    loaded it (or since the last okf_refresh): concepts added/removed/changed,
    type/title changes, links added/removed and newly broken/fixed. Call this
    to re-sync your understanding after files may have changed; if it shows
    changes, call okf_refresh to load them."""
    return R.diff(reg, bundle)


@mcp.tool()
def okf_refresh(bundle: Optional[str] = None) -> dict:
    """Re-ingest a directory-backed bundle so the catalog reflects the current
    files. Call after okf_diff reports changes, or when you know the bundle
    was edited since the server started."""
    return reg.refresh(bundle)


@mcp.tool()
def okf_doctor(bundle: Optional[str] = None, stale_days: Optional[int] = None) -> dict:
    """Health report for a bundle: score (0-100 = % of concepts with zero
    findings), error/warning counts, and per-rule counts (broken links,
    orphans, missing fields, duplicate titles; timestamps older than
    `stale_days` if given). Call before relying heavily on a bundle, or to
    answer 'is this knowledge base healthy'."""
    now = None
    if stale_days is not None:
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return R.doctor(reg, bundle, stale_days, now)


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    for a in args:
        if "=" in a and not a.split("=", 1)[0].startswith((".", "/", "\\")) \
                and ":" not in a.split("=", 1)[0]:
            name, source = a.split("=", 1)
        else:
            name, source = None, a
        if name is None:
            base = source.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
            name = base[:-7] if base.endswith(".duckdb") else base
        reg.add(name, source)
    try:
        mcp.run()          # stdio transport
    finally:
        reg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
