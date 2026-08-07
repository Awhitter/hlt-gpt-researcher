import asyncio
from types import SimpleNamespace

from gpt_researcher.skills.researcher import ResearchConductor


class MCPRetrieverFixture:
    pass


def test_mcp_only_research_never_falls_through_to_public_web(monkeypatch):
    researcher = SimpleNamespace(
        retrievers=[MCPRetrieverFixture],
        mcp_strategy="deep",
        mcp_only=True,
        verbose=False,
        websocket=None,
        context_manager=SimpleNamespace(),
    )
    conductor = ResearchConductor(researcher)

    async def mcp_results(_queries, _retrievers):
        return [
            {
                "content": "Exact source evidence",
                "url": "https://github.com/Awhitter/repo/blob/" + "a" * 40 + "/file.ts#L1",
                "title": "file.ts",
                "source_type": "mcp",
            }
        ]

    async def public_web_must_not_run(*_args, **_kwargs):
        raise AssertionError("public web search ran for an internal-only question")

    monkeypatch.setattr(conductor, "_execute_mcp_research_for_queries", mcp_results)
    monkeypatch.setattr(conductor, "_scrape_data_by_urls", public_web_must_not_run)

    result = asyncio.run(conductor._process_sub_query("Where is email captured?"))

    assert "Exact source evidence" in result
