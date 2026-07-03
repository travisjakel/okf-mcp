"""Offline tests: registry/tool logic on a tiny inline bundle, plus an
in-process MCP handshake (client <-> server over memory streams)."""
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from okf_mcp import registry as R  # noqa: E402


def make_bundle(tmp_path):
    d = tmp_path / "bundle"
    d.mkdir()
    (d / "index.md").write_text(textwrap.dedent("""\
        ---
        type: Index
        title: Home
        ---
        # Home
        - [Alpha](alpha.md)
        - [Beta](beta.md)
        """), encoding="utf-8")
    (d / "alpha.md").write_text(textwrap.dedent("""\
        ---
        type: Signal
        title: Alpha
        description: The alpha concept.
        ---
        # Alpha
        Depends on [Beta](beta.md). Mentions quicksilver.
        """), encoding="utf-8")
    (d / "beta.md").write_text(textwrap.dedent("""\
        ---
        type: Runbook
        title: Beta
        description: The beta concept.
        ---
        # Beta
        Body of beta.
        """), encoding="utf-8")
    return str(d)


@pytest.fixture()
def reg(tmp_path):
    r = R.BundleRegistry()
    r.add("test", make_bundle(tmp_path))
    yield r
    r.close()


def test_list_and_search(reg):
    bundles = R.list_bundles(reg)
    assert bundles[0]["name"] == "test" and bundles[0]["n_concepts"] == 2
    hits = R.search(reg, "quicksilver")
    assert [h["path"] for h in hits] == ["alpha.md"]


def test_get_concept_and_context(reg):
    c = R.get_concept(reg, "alpha.md")
    assert c["type"] == "Signal" and "quicksilver" in c["body"]
    ctx = R.context(reg, start="alpha.md", depth=1)
    assert "Alpha" in ctx["text"] and "beta.md" in ctx["included"]
    with pytest.raises(ValueError):
        R.get_concept(reg, "nope.md")


def test_impact_and_sql(reg):
    im = R.impact(reg, "beta.md")
    assert "alpha.md" in im["inbound"]
    rows = R.sql(reg, "SELECT count(*) AS n FROM okf_concept WHERE reserved = FALSE")
    assert rows[0]["n"] == 2
    with pytest.raises(ValueError):
        R.sql(reg, "DELETE FROM okf_concept")


def test_name_resolution(reg, tmp_path):
    # exact path, stem, title (case/punct-insensitive), and .md-less path all resolve
    b = reg.get()
    assert R.resolve(b, "alpha.md") == "alpha.md"
    assert R.resolve(b, "alpha") == "alpha.md"
    assert R.resolve(b, "ALPHA") == "alpha.md"
    # concept-addressed tools accept names
    assert R.get_concept(reg, "Alpha")["path"] == "alpha.md"
    assert "alpha.md" in R.impact(reg, "Beta")["inbound"]
    assert "beta.md" in R.context(reg, start="Alpha")["included"]
    # normalized title match: spaces vs underscores/punctuation
    (tmp_path / "bundle" / "multi_word_page.md").write_text(
        "---\ntype: Note\ntitle: Multi Word — Page!\n---\n# Multi Word\n", encoding="utf-8")
    reg.refresh()
    assert R.resolve(reg.get(), "multi word page") == "multi_word_page.md"
    # unknown -> helpful error; ambiguous -> lists candidates
    with pytest.raises(ValueError, match="okf_search"):
        R.resolve(reg.get(), "does not exist")
    (tmp_path / "bundle" / "sub").mkdir()
    (tmp_path / "bundle" / "sub" / "alpha2.md").write_text(
        "---\ntype: Note\ntitle: Alpha\n---\n# A2\n", encoding="utf-8")
    reg.refresh()
    with pytest.raises(ValueError, match="ambiguous"):
        R.resolve(reg.get(), "Alpha")            # two concepts titled Alpha
    assert R.resolve(reg.get(), "alpha") == "alpha.md"   # path shortcut still wins
    assert R.resolve(reg.get(), "alpha2") == "sub/alpha2.md"  # unique stem ok


def test_related_and_ppr_context(reg):
    rel = R.related(reg, "Beta", k=5)
    assert rel and rel[0]["path"] != "beta.md"          # seed excluded
    assert any(r["path"] == "alpha.md" for r in rel)     # linked neighbor ranked
    assert rel == R.related(reg, "beta", k=5)            # deterministic + name-resolved
    ctx = R.context(reg, start="Alpha")                  # default rank=ppr
    assert ctx["included"][0] == "alpha.md"
    ctx_bfs = R.context(reg, start="Alpha", rank="bfs")
    assert set(ctx_bfs["included"]) <= set(ctx["included"]) | {"alpha.md", "sub/b.md", "beta.md"}


def test_query_context(reg):
    ctx = R.context(reg, query="quicksilver mentions")
    assert "alpha.md" in ctx["included"]
    assert "alpha.md" in ctx["seeds"]
    with pytest.raises(ValueError, match="matched no concepts"):
        R.context(reg, query="zzz qqq")


def test_diff_refresh_doctor(reg, tmp_path):
    assert R.diff(reg)["identical"] is True
    # mutate the source dir -> drift shows, refresh clears it
    (tmp_path / "bundle" / "gamma.md").write_text(
        "---\ntype: Tool\ntitle: Gamma\n---\n# Gamma\n", encoding="utf-8")
    d = R.diff(reg)
    assert d["added"] == ["gamma.md"] and d["identical"] is False
    reg.refresh()
    assert R.diff(reg)["identical"] is True
    rep = R.doctor(reg)
    assert 0 <= rep["score"] <= 100


@pytest.mark.anyio
async def test_mcp_handshake(tmp_path):
    """Real MCP round-trip: server + client over in-memory streams."""
    from mcp.shared.memory import create_connected_server_and_client_session
    from okf_mcp import server as S

    S.reg.close()
    S.reg.bundles.clear()
    S.reg.add("test", make_bundle(tmp_path))
    try:
        async with create_connected_server_and_client_session(
                S.mcp._mcp_server) as session:
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {"okf_search", "okf_context", "okf_diff", "okf_doctor"} <= names
            res = await session.call_tool("okf_search", {"term": "quicksilver"})
            assert not res.isError
            assert "alpha.md" in res.content[0].text
    finally:
        S.reg.close()
        S.reg.bundles.clear()


@pytest.fixture
def anyio_backend():
    return "asyncio"
