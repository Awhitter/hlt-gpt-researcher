"""Unit tests for MCP tool scope/depth helpers."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.server import hlt_extensions
import mcp_server.tools as mcp_tools
from mcp_server.tools import (
    _build_research_scope,
    _prepare_scoped_request,
    _scope_summary,
    _source_limit_for_depth,
)


def test_build_research_scope_defaults_to_auto():
    assert _build_research_scope(None, "balanced") == {"auto": True, "depth": "balanced"}
    assert _build_research_scope("auto", "fast") == {"auto": True, "depth": "fast"}


def test_build_research_scope_none_forces_web_only():
    assert _build_research_scope("none", "deep") == {"memory": False, "depth": "deep"}


def test_build_research_scope_pins_valid_keys():
    scope = _build_research_scope(["codebase", "cms", "nope"], "balanced")
    assert scope == {"codebase": True, "cms": True, "depth": "balanced"}


def test_build_research_scope_accepts_single_string_key():
    scope = _build_research_scope("metrics", "bogus")
    assert scope == {"metrics": True, "depth": "balanced"}


def test_scope_summary_exposes_auto_fields():
    summary = _scope_summary(
        {
            "active_sources": ["codebase"],
            "degraded_sources": [],
            "mcp_server_count": 2,
            "depth": "fast",
            "auto_scope": {
                "requested": True,
                "applied": ["codebase"],
                "reasons": {"codebase": ["mentions ScraperVault"]},
            },
        }
    )
    assert summary == {
        "active_sources": ["codebase"],
        "degraded_sources": [],
        "auto": {
            "requested": True,
            "applied": ["codebase"],
            "reasons": {"codebase": ["mentions ScraperVault"]},
        },
        "mcp_server_count": 2,
        "depth": "fast",
    }


def test_source_limit_tracks_depth_and_bounds_explicit_overrides():
    assert _source_limit_for_depth("fast") == 5
    assert _source_limit_for_depth("balanced") == 8
    assert _source_limit_for_depth("deep") == 12
    assert _source_limit_for_depth("bogus") == 8
    assert _source_limit_for_depth("deep", 2) == 3
    assert _source_limit_for_depth("fast", 99) == 20
    assert _source_limit_for_depth("fast", 9) == 9


@pytest.fixture
def scoped_request_memory(monkeypatch):
    """Keep request preparation real; replace report storage and readiness I/O."""
    prior_report = {
        "id": "private-report-fixture-id",
        "question": "Confidential research fixture question",
        "date": "2099-04-05",
    }
    lookups = []

    def search_prior_reports(query, *, limit):
        lookups.append((query, limit))
        return [prior_report]

    monkeypatch.setattr(hlt_extensions, "search_prior_reports", search_prior_reports)
    monkeypatch.setattr(
        hlt_extensions,
        "get_hlt_readiness",
        lambda: {
            "integrations": {
                key: {"status": "unavailable"} for key in mcp_tools._VALID_SCOPE_KEYS
            },
            "preset_statuses": {},
            "scraper": {"status": "unavailable"},
        },
    )
    monkeypatch.setattr(
        hlt_extensions,
        "infer_research_scope",
        lambda *_args, **_kwargs: {
            "scopes": [], "reasons": {}, "llm_used": False, "skipped_unready": []
        },
    )
    return prior_report, lookups


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", ["fast", "balanced", "deep"])
async def test_quick_search_none_never_reads_or_sends_report_memory(
    monkeypatch, scoped_request_memory, depth
):
    prior_report, lookups = scoped_request_memory
    queries = []

    class SearchRecorder:
        def __init__(self, *, query, **_kwargs):
            queries.append(query)
            self.cfg = SimpleNamespace()

        async def quick_search(self, *, query, **_kwargs):
            queries.append(query)
            return []

    monkeypatch.setattr(mcp_tools, "GPTResearcher", SearchRecorder)
    mcp = mcp_tools.FastMCP("scope-memory-regression")
    mcp_tools.register_tools(mcp)
    quick_search = mcp._tool_manager.get_tool("quick_search").fn
    query = "What is the public evidence for urban tree cooling?"

    result = await quick_search(query, scope="none", depth=depth)

    assert result["status"] == "success"
    assert result["mode"] == "web"
    assert result["hlt_scope"]["auto"]["requested"] is False
    assert lookups == []
    assert len(queries) == 2
    for outgoing_query in queries:
        assert query in outgoing_query
        assert all(value not in outgoing_query for value in prior_report.values())


@pytest.mark.parametrize("scope", [None, "auto", ["recruiting"]])
def test_other_scopes_keep_report_memory(scoped_request_memory, scope):
    prior_report, lookups = scoped_request_memory
    query = "What is the public evidence for urban tree cooling?"

    task, _configs, _strategy, _scraper, metadata = _prepare_scoped_request(
        query, _build_research_scope(scope, "balanced")
    )

    assert lookups == [(query, 3)]
    assert metadata["prior_research"] == [prior_report]
    assert all(value in task for value in prior_report.values())
    assert metadata["auto_scope"]["requested"] is (scope in (None, "auto"))
