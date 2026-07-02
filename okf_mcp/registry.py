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
        "FROM okf_concept WHERE path = ?", [path]))
    if not rows:
        raise ValueError(f"concept not found: {path}")
    return rows[0]


def context(reg: BundleRegistry, start: Optional[str] = None, depth: int = 1,
            max_tokens: int = 8000, bundle: Optional[str] = None) -> dict:
    b = reg.get(bundle)
    return okf.context(b.con, start=start, depth=depth, max_tokens=max_tokens)


def impact(reg: BundleRegistry, concept: str, bundle: Optional[str] = None) -> dict:
    b = reg.get(bundle)
    return okf_impact(b.con, concept)


def backlinks(reg: BundleRegistry, concept: str, bundle: Optional[str] = None) -> list:
    b = reg.get(bundle)
    return okf_backlinks(b.con, concept)


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
