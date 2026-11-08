"""A code question asked through MCP keeps its code evidence and stays off the public web.

8 Nov 2026: "In the Vault repo, which file builds the weekly jobs digest Listmonk draft?"
through Katailyst came back with no context. Codegraph's search matches carry an
internal mcp:// reference, which the public-URL policy rejected, and the MCP server
never switched code questions to internal-only, so the pass scraped listmonk.app.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.server.hlt_extensions import is_internal_only_research
from gpt_researcher.skills.researcher import ResearchConductor
from gpt_researcher.source_policy import SourcePolicy
from mcp_server import tools as mcp_tools

from tests.test_mcp_persistence import FakeGPTResearcher, _reset_store


class _Retriever:
    pass


def _conductor(policy: SourcePolicy) -> ResearchConductor:
    researcher = SimpleNamespace(
        source_policy=policy,
        verbose=False,
        websocket=None,
        visited_urls=set(),
        sources=[],
        add_research_sources=lambda rows: researcher.sources.extend(rows),
        add_source_rejection=lambda *args, **kwargs: None,
    )
    return ResearchConductor(researcher)


def _results(conductor: ResearchConductor) -> list[dict]:
    async def execute(_retriever, _query):
        return [
            {"title": "search_source", "href": "mcp://search_source", "body": "weekly-digest-panel.tsx:129 listmonk"},
            {"title": "LLM Analysis", "href": "mcp://llm_analysis", "body": "The digest draft is built in jobs-digest.ts"},
            {"title": "read_source", "href": "https://github.com/Awhitter/ScraperVault/blob/" + "a" * 40 + "/x.ts#L1-L9", "body": "export function buildDigest()"},
            {"title": "web page", "href": "https://blocked.example/page", "body": "unrelated"},
        ]

    conductor._execute_mcp_research = execute
    return asyncio.run(conductor._execute_mcp_research_for_queries(["q"], [_Retriever]))


def test_code_search_matches_and_analysis_reach_the_context():
    context = _results(_conductor(SourcePolicy.from_value({"denied_domains": ["blocked.example"]})))

    assert [row["url"] for row in context] == [
        "mcp://search_source",
        "mcp://llm_analysis",
        "https://github.com/Awhitter/ScraperVault/blob/" + "a" * 40 + "/x.ts#L1-L9",
    ]


def test_a_strict_public_contract_still_drops_internal_references():
    policy = SourcePolicy.from_value({"enforcement": "strict", "required_sources": ["https://example.com/a"]})

    assert all(not row["url"].startswith("mcp://") for row in _results(_conductor(policy)))


def test_code_scope_is_internal_only_unless_web_sources_are_asked_for():
    assert is_internal_only_research(True, {"active_sources": ["codebase"]})
    assert not is_internal_only_research(True, {"active_sources": ["codebase", "firecrawl"]})
    assert not is_internal_only_research(False, {"active_sources": ["codebase"]})
    assert not is_internal_only_research(True, None)


def test_an_mcp_code_question_runs_internal_only(monkeypatch, tmp_path):
    _reset_store(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", FakeGPTResearcher)
    monkeypatch.setattr(
        mcp_tools,
        "_prepare_scoped_request",
        lambda query, _scope: (query, [{"name": "codegraph"}], "deep", None, {"active_sources": ["codebase"]}),
    )

    item, _scope = asyncio.run(mcp_tools._conduct_research("Which file builds the digest?", scope="codebase", depth="fast"))

    assert item.researcher.mcp_only is True


def test_a_web_question_keeps_the_public_web(monkeypatch, tmp_path):
    _reset_store(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", FakeGPTResearcher)
    monkeypatch.setattr(mcp_tools, "_prepare_scoped_request", lambda query, _scope: (query, [], None, None, {"active_sources": []}))

    item, _scope = asyncio.run(mcp_tools._conduct_research("Nurse staffing ratios in Iowa", scope="none"))

    assert item.researcher.mcp_only is False
