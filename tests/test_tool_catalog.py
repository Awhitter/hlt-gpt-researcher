"""Keep the Katailyst capability doc and MCP catalog in lockstep."""

from __future__ import annotations

from pathlib import Path

from mcp_server.tool_catalog import PRODUCT_MCP_TOOLS, RESEARCH_MCP_TOOLS, research_capabilities

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_capability_doc_lists_every_research_tool():
    text = (REPO_ROOT / "scripts" / "katailyst_capability.md").read_text(encoding="utf-8")
    for name in RESEARCH_MCP_TOOLS:
        assert f"`{name}`" in text, f"scripts/katailyst_capability.md omitted {name}"
    assert "hlt-gpt-researcher-1" not in text
    assert "Bind the hosted MCP" in text


def test_catalog_matches_tools_module():
    source = (REPO_ROOT / "mcp_server" / "tools.py").read_text(encoding="utf-8")
    for name in RESEARCH_MCP_TOOLS:
        assert f"async def {name}(" in source, f"catalog lists {name} but tools.py has no function"
    product_source = (REPO_ROOT / "mcp_server" / "product_tools.py").read_text(encoding="utf-8")
    for name in PRODUCT_MCP_TOOLS:
        assert f"def {name}(" in product_source, f"catalog lists {name} but product_tools.py has no function"


def test_capabilities_payload_tells_agents_not_to_use_rest_quick_search():
    payload = research_capabilities()
    assert payload["bind"] == "mcp"
    assert payload["doors"]["quick_search"]["estate"] is False
    assert payload["doors"]["gather"]["estate"] is False
    assert payload["doors"]["mcp"]["estate"] is True
    assert payload["mcp"]["tools"] == list(RESEARCH_MCP_TOOLS)


def test_agents_md_says_research_pins_the_k2_graph():
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "discover" in text
    assert "KATAILYST_TOOLSET=bootstrap" in text
