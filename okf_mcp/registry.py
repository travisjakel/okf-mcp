"""okf-mcp — bundle registry and tool logic.

Plain functions over a BundleRegistry so the logic is testable without an MCP
transport; okf_mcp.server wraps these as MCP tools. All operations are
READ-ONLY with respect to the source bundles: directory sources are ingested
into disposable in-memory DuckDB catalogs at startup (refresh re-ingests),
.duckdb sources are opened read-only. Everything is deterministic okf-ingest
code — no model calls.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

import duckdb
import okf
from okf.diff import diff as okf_diff
from okf.doctor import doctor as okf_doctor
from okf.graph import backlinks as okf_backlinks, impact as okf_impact


@dataclass
class Bundle:
    name: str
    source: str          # directory or .duckdb path
    kind: str            # "dir" | "catalog"
    con: Any             # duckdb connection


class BundleRegistry:
    """Named OKF bundles the server exposes. The first added is the default."""

    def __init__(self) -> None:
        self.bundles: dict[str, Bundle] = {}

    def add(self, name: str, source: str) -> Bundle:
        if name in self.bundles:
            raise ValueError(f"duplicate bundle name: {name}")
        source = os.path.abspath(source)
        if source.endswith(".duckdb") and os.path.isfile(source):
            con = duckdb.connect(source, read_only=True)
            b = Bundle(name, source, "catalog", con)
        elif os.path.isdir(source):
            con, _ = okf.ingest(source)   # in-memory catalog
            b = Bundle(name, source, "dir", con)
        else:
            raise ValueError(f"not a bundle dir or .duckdb catalog: {source}")
        self.bundles[name] = b
        return b

    def get(self, name: Optional[str] = None) -> Bundle:
        if not self.bundles:
            raise RuntimeError("no bundles loaded")
        if name is None:
            return next(iter(self.bundles.values()))
        if name not in self.bundles:
            raise ValueError(
                f"unknown bundle {name!r}; available: {', '.join(self.bundles)}")
        return self.bundles[name]

    def refresh(self, name: Optional[str] = None) -> dict:
        b = self.get(name)
        if b.kind != "dir":
            raise ValueError(f"bundle {b.name!r} is a static catalog; nothing to refresh")
        old = b.con
        b.con, summary = okf.ingest(b.source)
        old.close()
        keep = ("n_concepts", "conformant", "links_total", "links_broken")
        return {"bundle": b.name, **{k: v for k, v in summary.items() if k in keep}}

    def close(self) -> None:
        for b in self.bundles.values():
            try:
                b.con.close()
            except Exception:
                pass


# ---- tool logic -------------------------------------------------------------

def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _norm(s: str) -> str:
    """Comparison key: lowercase, alphanumerics only ('Agent Memory — arch.' ==
    'agent_memory_arch')."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def resolve(b: Bundle, ref: str) -> str:
    """Resolve a concept reference to its bundle path, deterministically.

    Precedence mirrors okf-ingest's [[wikilink]] resolver: exact path ->
    frontmatter id -> alias -> title -> filename stem (each tier compared
    case-insensitively on alphanumerics, so 'agent memory architecture'
    matches ops/agent_memory_architecture.md). A tier with multiple matches
    raises with the candidates rather than guessing; no match raises with a
    hint to use search.
    """
    import json

    ref = ref.strip()
    rows = _rows(b.con.execute(
        "SELECT path, title, frontmatter FROM okf_concept ORDER BY path"))
    known = {r["path"] for r in rows}
    if ref in known:
        return ref
    if not ref.endswith(".md") and f"{ref}.md" in known:
        return f"{ref}.md"

    key = _norm(ref)
    tiers: dict[str, list[str]] = {"id": [], "alias": [], "title": [], "stem": []}
    for r in rows:
        fm = {}
        try:
            fm = json.loads(r["frontmatter"] or "{}")
        except Exception:
            pass
        if fm.get("id") is not None and _norm(str(fm["id"])) == key:
            tiers["id"].append(r["path"])
        for a in (fm.get("aliases") or []):
            if _norm(str(a)) == key:
                tiers["alias"].append(r["path"])
        if r["title"] and _norm(r["title"]) == key:
            tiers["title"].append(r["path"])
        stem = r["path"].rsplit("/", 1)[-1]
        stem = stem[:-3] if stem.endswith(".md") else stem
        if _norm(stem) == key:
            tiers["stem"].append(r["path"])
    for tier in ("id", "alias", "title", "stem"):
        hits = sorted(set(tiers[tier]))
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ValueError(
                f"ambiguous concept {ref!r} (by {tier}): {', '.join(hits)} — pass the exact path")
    raise ValueError(
        f"concept not found: {ref!r} — try okf_search to locate it, or pass a bundle-relative path")


def list_bundles(reg: BundleRegistry) -> list[dict]:
    out = []
    for b in reg.bundles.values():
        n = b.con.execute(
            "SELECT count(*) FROM okf_concept WHERE reserved = FALSE").fetchone()[0]
        out.append({"name": b.name, "source": b.source, "kind": b.kind,
                    "n_concepts": n, "default": b is next(iter(reg.bundles.values()))})
    return out


def search(reg: BundleRegistry, term: str, bundle: Optional[str] = None,
           limit: int = 20) -> list[dict]:
    b = reg.get(bundle)
    return _rows(b.con.execute(
        "SELECT path, type, title, description FROM okf_concept "
        "WHERE body ILIKE ? OR title ILIKE ? ORDER BY path LIMIT ?",
        [f"%{term}%", f"%{term}%", int(limit)]))


def sql(reg: BundleRegistry, query: str, bundle: Optional[str] = None) -> list[dict]:
    q = query.lstrip().lower()
    if not (q.startswith("select") or q.startswith("with")):
        raise ValueError("only SELECT/WITH queries are allowed")
    b = reg.get(bundle)
    return _rows(b.con.execute(query))


def get_concept(reg: BundleRegistry, path: str, bundle: Optional[str] = None) -> dict:
    b = reg.get(bundle)
    rows = _rows(b.con.execute(
        "SELECT path, type, title, description, tags, timestamp, body "
        "FROM okf_concept WHERE path = ?", [resolve(b, path)]))
    return rows[0]


def context(reg: BundleRegistry, start: Optional[str] = None, depth: int = 1,
            max_tokens: int = 8000, bundle: Optional[str] = None) -> dict:
    b = reg.get(bundle)
    if start is not None:
        start = resolve(b, start)
    return okf.context(b.con, start=start, depth=depth, max_tokens=max_tokens)


def impact(reg: BundleRegistry, concept: str, bundle: Optional[str] = None) -> dict:
    b = reg.get(bundle)
    return okf_impact(b.con, resolve(b, concept))


def backlinks(reg: BundleRegistry, concept: str, bundle: Optional[str] = None) -> list:
    b = reg.get(bundle)
    return okf_backlinks(b.con, resolve(b, concept))


def diff(reg: BundleRegistry, bundle: Optional[str] = None) -> dict:
    b = reg.get(bundle)
    if b.kind != "dir":
        raise ValueError(
            f"bundle {b.name!r} is a static catalog; drift-diff needs a directory source")
    return okf_diff(b.con, b.source)


def doctor(reg: BundleRegistry, bundle: Optional[str] = None,
           stale_days: Optional[int] = None, now: Optional[str] = None) -> dict:
    b = reg.get(bundle)
    return okf_doctor(b.con, now=now, stale_days=stale_days)
