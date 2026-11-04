"""Exercise quick-search limits without HTTP or model calls."""

import json
from types import SimpleNamespace

import pytest

import gpt_researcher.agent as agent_module
from gpt_researcher.actions.query_processing import get_search_results
from gpt_researcher.retrievers.firecrawl.firecrawl_search import FirecrawlSearch


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [3, 7])
@pytest.mark.parametrize("aggregated_summary", [False, True])
async def test_quick_search_limits_provider_request_and_consumed_results(
    monkeypatch, limit, aggregated_summary
):
    requests = []
    completions = []
    provider_results = [
        {"url": f"https://example.com/{index}", "title": f"Source {index}"}
        for index in range(9)
    ]

    def post(url, **kwargs):
        requests.append((url, kwargs))
        # An adapter/provider may return more results than requested.
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": provider_results},
        )

    async def complete(**kwargs):
        completions.append(kwargs)
        return "Offline summary"

    monkeypatch.setenv("FIRECRAWL_API_KEY", "offline-test")
    monkeypatch.setenv("FIRECRAWL_SERVER_URL", "https://firecrawl.invalid")
    monkeypatch.setattr(
        "gpt_researcher.retrievers.firecrawl.firecrawl_search.requests.post", post
    )
    monkeypatch.setattr(agent_module, "create_chat_completion", complete)
    researcher = agent_module.GPTResearcher.__new__(agent_module.GPTResearcher)
    researcher.retrievers = [FirecrawlSearch]
    researcher.cfg = SimpleNamespace(
        max_search_results_per_query=limit,
        smart_llm_model="offline-model",
        smart_llm_provider="offline-provider",
        smart_token_limit=50,
        llm_kwargs={},
    )
    researcher.prompt_family = SimpleNamespace(
        generate_quick_summary_prompt=lambda query, context: context
    )

    result = await researcher.quick_search(
        "test query", query_domains=["example.com"], aggregated_summary=aggregated_summary
    )

    assert len(requests) == 1
    assert requests[0][0] == "https://firecrawl.invalid/v1/search"
    assert json.loads(requests[0][1]["data"]) == {
        "query": "test query (site:example.com)",
        "limit": limit,
    }
    if aggregated_summary:
        assert result == "Offline summary"
        assert len(completions) == 1
        context = completions[0]["messages"][0]["content"]
        for index in range(9):
            assert (f"Source {index}:" in context) == (index < limit)
    else:
        assert [item["title"] for item in result] == [
            f"Source {index}" for index in range(limit)
        ]
        assert completions == []


@pytest.mark.asyncio
async def test_search_helper_preserves_omitted_limit_and_legacy_signature():
    results = [{"title": f"Source {index}"} for index in range(12)]

    class LegacyRetriever:
        def __init__(self, query, query_domains=None):
            assert query == "test query"
            assert query_domains == ["example.com"]

        def search(self):
            return results

    assert await get_search_results(
        "test query", LegacyRetriever, query_domains=["example.com"]
    ) is results


@pytest.mark.asyncio
async def test_search_helper_preserves_mcp_context_with_explicit_limit():
    owner = object()
    results = [{"title": f"Source {index}"} for index in range(5)]

    class TestMCPRetriever:
        def __init__(self, query, query_domains=None, researcher=None):
            assert query == "test query"
            assert query_domains == ["example.com"]
            assert researcher is owner

        def search(self, max_results=10):
            assert max_results == 3
            return results

    assert await get_search_results(
        "test query", TestMCPRetriever, query_domains=["example.com"],
        researcher=owner, max_results=3,
    ) == results[:3]


@pytest.mark.asyncio
async def test_search_helper_preserves_absent_provider_results():
    class EmptyRetriever:
        def __init__(self, query, query_domains=None):
            pass

        def search(self, max_results=5):
            assert max_results == 3
            return None

    assert await get_search_results("test query", EmptyRetriever, max_results=3) is None
