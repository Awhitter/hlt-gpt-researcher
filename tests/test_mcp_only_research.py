import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage

from gpt_researcher.mcp.research import MCPResearchSkill
from gpt_researcher.mcp.tool_selector import MCPToolSelector
from gpt_researcher.prompts import PromptFamily
from gpt_researcher.retrievers.mcp.retriever import MCPRetriever
from gpt_researcher.skills.researcher import (
    ResearchConductor,
    extract_numbered_questions,
)


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


def test_mcp_only_planning_does_not_call_a_public_search_provider(monkeypatch):
    researcher = SimpleNamespace(
        retrievers=[MCPRetrieverFixture],
        mcp_only=True,
        websocket=None,
        role="Internal research teammate",
        cfg=SimpleNamespace(),
        parent_query="",
        report_type="research_report",
        kwargs={},
        add_costs=lambda *_args, **_kwargs: None,
    )
    conductor = ResearchConductor(researcher)

    async def public_web_must_not_run(*_args, **_kwargs):
        raise AssertionError("public web search ran during internal-only planning")

    async def plan_from_internal_context(**kwargs):
        assert kwargs["search_results"] == []
        return [kwargs["query"]]

    monkeypatch.setattr(
        "gpt_researcher.skills.researcher.get_search_results",
        public_web_must_not_run,
    )
    monkeypatch.setattr(
        "gpt_researcher.skills.researcher.plan_research_outline",
        plan_from_internal_context,
    )

    result = asyncio.run(conductor.plan_research("Where is email captured?"))

    assert result == ["Where is email captured?"]


def test_explicit_numbered_questions_keep_their_system_targets():
    query = (
        "Please answer briefly: 1) What attributes do we capture for a nurse, "
        "and which system owns each kind? 2) Exactly when and where do we capture "
        "a nurse email? 3) How does Nursing Mastery job search work? 4) What "
        "onboarding questions do we currently ask? 5) Do we currently store or "
        "send these emails in Marketo? If a source is unavailable, say so."
    )

    assert extract_numbered_questions(query) == [
        "What attributes do we capture for a nurse, and which system owns each kind?",
        "Exactly when and where do we capture a nurse email?",
        "How does Nursing Mastery job search work?",
        "What onboarding questions do we currently ask?",
        "Do we currently store or send these emails in Marketo?",
    ]


def test_numbered_questions_ignore_replayed_prior_research_context():
    current = (
        "1) What attributes do we capture? 2) When do we capture email? "
        "3) How does job search work? 4) What onboarding questions do we ask? "
        "5) Do we use Marketo?"
    )
    query = (
        f"Please answer: {current} HLT research scope instructions: prior "
        f"research exists on: {current}"
    )

    assert extract_numbered_questions(query) == [
        "What attributes do we capture?",
        "When do we capture email?",
        "How does job search work?",
        "What onboarding questions do we ask?",
        "Do we use Marketo?",
    ]


def test_mcp_only_planning_does_not_rewrite_numbered_questions(monkeypatch):
    query = (
        "Please answer: 1) How does Nursing Mastery job search work? "
        "2) Do we send email to Marketo?"
    )
    researcher = SimpleNamespace(
        retrievers=[MCPRetrieverFixture],
        mcp_only=True,
        websocket=None,
    )
    conductor = ResearchConductor(researcher)

    async def planner_must_not_run(**_kwargs):
        raise AssertionError("the LLM planner rewrote explicit numbered questions")

    monkeypatch.setattr(
        "gpt_researcher.skills.researcher.plan_research_outline",
        planner_must_not_run,
    )

    result = asyncio.run(conductor.plan_research(query))

    assert result == [
        "How does Nursing Mastery job search work?",
        "Do we send email to Marketo?",
    ]


def test_mcp_numbered_context_does_not_repeat_the_umbrella_query(monkeypatch):
    query = "1) Where is email captured? 2) Do we use Marketo?"
    researcher = SimpleNamespace(
        retrievers=[],
        report_type="research_report",
        mcp_only=True,
        mcp_strategy="disabled",
        verbose=False,
        websocket=None,
    )
    conductor = ResearchConductor(researcher)
    processed = []

    async def fake_plan_research(_query, _query_domains=None):
        return ["Where is email captured?", "Do we use Marketo?"]

    async def fake_process(sub_query, _scraped_data, _query_domains):
        processed.append(sub_query)
        return sub_query

    monkeypatch.setattr(conductor, "plan_research", fake_plan_research)
    monkeypatch.setattr(conductor, "_process_sub_query", fake_process)

    result = asyncio.run(conductor._get_context_by_web_search(query))

    assert processed == ["Where is email captured?", "Do we use Marketo?"]
    assert query not in result


def test_mcp_tool_discovery_retries_one_empty_listing():
    retriever = object.__new__(MCPRetriever)
    retriever._all_tools_cache = None
    retriever.streamer = SimpleNamespace(
        stream_log=AsyncMock(),
        stream_warning=AsyncMock(),
    )
    listings = [[], ["search_source", "read_source"]]

    async def get_all_tools():
        return listings.pop(0)

    retriever.client_manager = SimpleNamespace(
        get_all_tools=get_all_tools,
        close_client=AsyncMock(),
    )

    result = asyncio.run(retriever._get_all_tools())

    assert result == ["search_source", "read_source"]
    assert retriever.client_manager.close_client.await_count == 1
    assert retriever._all_tools_cache == result


class FakeTool:
    def __init__(self, name, result=""):
        self.name = name
        self.description = f"Description for {name}"
        self.result = result
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self.result


def test_code_research_prompt_follows_authority_and_capture_write_paths():
    prompt = PromptFamily.generate_mcp_research_prompt(
        "Who owns email and exactly when is it captured?",
        [FakeTool("search_source"), FakeTool("read_source")],
    )

    assert "does not prove data ownership" in prompt
    assert "find the submission handler" in prompt
    assert "search for that symbol and open its implementation" in prompt
    assert "A required database field alone is not evidence" in prompt
    assert "do not infer a permanent or system-wide absence" in prompt


def test_code_only_selection_always_includes_source_discovery_and_reading():
    tools = [
        FakeTool("list_repos"),
        FakeTool("search_source"),
        FakeTool("read_source"),
        FakeTool("verify_source_ref"),
        FakeTool("search_registry"),
    ]

    selected = MCPToolSelector.ensure_code_source_tools(
        [tools[0], tools[4]],
        tools,
        max_tools=5,
    )

    assert [tool.name for tool in selected] == [
        "search_source",
        "read_source",
        "verify_source_ref",
        "list_repos",
        "search_registry",
    ]


def test_registry_selection_pins_katailyst_graph_not_generic_search():
    tools = [
        FakeTool("web_search"),
        FakeTool("list_repos"),
        FakeTool("discover"),
        FakeTool("traverse"),
        FakeTool("get_entity"),
        FakeTool("tool_search"),
        FakeTool("tool_execute"),
        FakeTool("memory_query"),
    ]

    selected = MCPToolSelector.ensure_katailyst_registry_tools(
        [tools[0], tools[1]],
        tools,
        max_tools=8,
    )

    assert [tool.name for tool in selected][:5] == [
        "discover",
        "traverse",
        "get_entity",
        "tool_search",
        "tool_execute",
    ]
    assert "web_search" in [tool.name for tool in selected]


def test_estate_selection_keeps_code_and_registry_tools_together():
    tools = [
        FakeTool("web_search"),
        FakeTool("search_source"),
        FakeTool("read_source"),
        FakeTool("verify_source_ref"),
        FakeTool("discover"),
        FakeTool("traverse"),
        FakeTool("get_entity"),
        FakeTool("tool.search"),
    ]

    selected = MCPToolSelector.ensure_estate_research_tools(
        [tools[0]],
        tools,
        max_tools=10,
    )
    names = [tool.name for tool in selected]

    assert names[:3] == ["search_source", "read_source", "verify_source_ref"]
    assert names[3:6] == ["discover", "traverse", "get_entity"]
    assert "tool.search" in names
    assert "web_search" in names


def test_registry_research_prompt_is_not_generic_web_search():
    prompt = PromptFamily.generate_mcp_research_prompt(
        "What skills exist for nurse recruiting?",
        [FakeTool("discover"), FakeTool("get_entity")],
    )

    assert "KATAILYST2 REGISTRY" in prompt
    assert "capability graph" in prompt
    assert "never write, publish, or orchestrate" in prompt


def test_mcp_research_can_search_then_read_a_discovered_source(monkeypatch):
    search_tool = FakeTool(
        "search_source",
        '{"results":[{"repo":"nursing-mastery","path":"app/api/identity/route.ts"}]}',
    )
    read_tool = FakeTool(
        "read_source",
        '{"repo":"nursing-mastery","path":"app/api/identity/route.ts",'
        '"url":"https://github.com/Awhitter/nursing-mastery/blob/'
        + "a" * 40
        + '/app/api/identity/route.ts#L148","content":"consentStatus: accepted"}',
    )

    responses = iter(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_source",
                        "args": {"query_text": "email consent capture"},
                        "id": "search-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_source",
                        "args": {
                            "repo": "nursing-mastery",
                            "path": "app/api/identity/route.ts",
                            "start_line": 140,
                            "end_line": 170,
                        },
                        "id": "read-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="The exact identity route records accepted consent."),
        ]
    )

    class FakeBoundLLM:
        def bind_tools(self, tools):
            assert [tool.name for tool in tools] == ["search_source", "read_source"]
            return self

        async def ainvoke(self, _messages):
            return next(responses)

    monkeypatch.setattr(
        "gpt_researcher.llm_provider.generic.base.GenericLLMProvider.from_provider",
        lambda *_args, **_kwargs: SimpleNamespace(llm=FakeBoundLLM()),
    )
    cfg = SimpleNamespace(
        strategic_llm_model="test-model",
        strategic_llm_provider="test-provider",
        llm_kwargs={},
    )

    results = asyncio.run(
        MCPResearchSkill(cfg).conduct_research_with_tools(
            "Where is email consent captured?",
            [search_tool, read_tool],
        )
    )

    assert search_tool.calls == [{"query_text": "email consent capture"}]
    assert read_tool.calls == [
        {
            "repo": "nursing-mastery",
            "path": "app/api/identity/route.ts",
            "start_line": 140,
            "end_line": 170,
        }
    ]
    assert any("consentStatus: accepted" in item["body"] for item in results)
    assert results[-1]["body"] == "The exact identity route records accepted consent."
    read_result = next(item for item in results if item["tool_name"] == "read_source")
    assert read_result["href"].startswith(
        "https://github.com/Awhitter/nursing-mastery/blob/"
    )


def test_mcp_research_auto_opens_matches_when_model_repeats_search(monkeypatch):
    source_url = (
        "https://github.com/Awhitter/nursing-mastery/blob/"
        + "a" * 40
        + "/app/api/identity/route.ts#L148"
    )
    search_tool = FakeTool(
        "search_source",
        [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "matches": [
                            {
                                "repo": "Awhitter/nursing-mastery",
                                "commitSha": "a" * 40,
                                "path": "app/api/identity/route.ts",
                                "line": 148,
                                "authority": 3,
                                "score": 4,
                                "url": source_url,
                            }
                        ]
                    }
                ),
            }
        ],
    )
    read_tool = FakeTool(
        "read_source",
        json.dumps(
            {
                "repo": "Awhitter/nursing-mastery",
                "commitSha": "a" * 40,
                "path": "app/api/identity/route.ts",
                "startLine": 118,
                "endLine": 208,
                "url": source_url + "-L208",
                "lines": [{"line": 148, "text": "consentStatus: accepted"}],
            }
        ),
    )

    responses = iter(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_source",
                        "args": {"query_text": "email consent capture"},
                        "id": "search-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_source",
                        "args": {"query_text": "identity email consent"},
                        "id": "search-2",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="The opened route records consent."),
        ]
    )

    class RepeatingSearchLLM:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            return next(responses)

    monkeypatch.setattr(
        "gpt_researcher.llm_provider.generic.base.GenericLLMProvider.from_provider",
        lambda *_args, **_kwargs: SimpleNamespace(llm=RepeatingSearchLLM()),
    )
    cfg = SimpleNamespace(
        strategic_llm_model="test-model",
        strategic_llm_provider="test-provider",
        llm_kwargs={},
    )

    results = asyncio.run(
        MCPResearchSkill(cfg, SimpleNamespace(mcp_only=True)).conduct_research_with_tools(
            "Where is email consent captured?",
            [search_tool, read_tool],
        )
    )

    assert read_tool.calls == [
        {
            "repo": "Awhitter/nursing-mastery",
            "path": "app/api/identity/route.ts",
            "start_line": 118,
            "end_line": 208,
        }
    ]
    opened = [item for item in results if item.get("tool_name") == "read_source"]
    assert len(opened) == 1
    assert "consentStatus: accepted" in opened[0]["body"]


def test_nested_mcp_text_payload_preserves_exact_read_source_url():
    url = (
        "https://github.com/Awhitter/nursing-mastery/blob/"
        + "a" * 40
        + "/app/api/identity/route.ts#L148-L170"
    )
    nested = {
        "content": [
            {
                "type": "text",
                "text": json.dumps({
                    "repo": "Awhitter/nursing-mastery",
                    "commitSha": "a" * 40,
                    "path": "app/api/identity/route.ts",
                    "url": url,
                    "content": "consentStatus: accepted",
                }),
            }
        ]
    }

    result = MCPResearchSkill(SimpleNamespace())._process_tool_result(
        "read_source",
        nested,
    )

    assert result[0]["href"] == url
    assert result[0]["tool_name"] == "read_source"


def test_hlt_nurse_questions_get_precise_source_discovery_seeds():
    marker = "\n\nHLT research scope instructions:\n- Keep this internal."

    assert MCPResearchSkill._hlt_source_search_seeds(
        "How does Nursing Mastery job search work?" + marker
    ) == ["getJobs getCuratedJobs getRankedJobs job search"]
    assert MCPResearchSkill._hlt_source_search_seeds(
        "Exactly when and where do we capture a nurse email?" + marker
    ) == ["QuickStartBridge EmailCaptureCard POST /api/identity email capture"]
    assert MCPResearchSkill._hlt_source_search_seeds(
        "Do we store or send these emails in Marketo?" + marker
    ) == ["growth-signal-sync upsertMarketoLeadByEmail personKey marketoLeadId"]
    assert MCPResearchSkill._hlt_source_search_seeds(
        "What attributes do we capture for a nurse, and which system owns each kind?"
        + marker
    ) == [
        "Identity UserProfile UserConsent SvAccountDataSync user_preferences career preferences",
        "DashboardResponse profile preferences consent getProfileBlueprint nurse",
    ]
    assert MCPResearchSkill._hlt_source_search_seeds(
        "How does job search work?"
    ) == []


def test_hlt_child_question_uses_parent_scope_for_source_discovery():
    skill = MCPResearchSkill(
        SimpleNamespace(),
        SimpleNamespace(
            query=(
                "Please answer five Nursing Mastery questions.\n\n"
                "HLT research scope instructions:\n- Use connected code sources."
            ),
            parent_query="",
        ),
    )

    assert skill._hlt_source_search_seeds(
        "Exactly when and where do we capture a nurse email?",
        hlt_scoped=skill._has_hlt_scope_context(
            "Exactly when and where do we capture a nurse email?"
        ),
    ) == ["QuickStartBridge EmailCaptureCard POST /api/identity email capture"]


def test_generic_child_question_does_not_activate_hlt_source_discovery():
    skill = MCPResearchSkill(
        SimpleNamespace(),
        SimpleNamespace(
            query="How does a generic job search product work?",
            parent_query="",
        ),
    )

    assert not skill._has_hlt_scope_context(
        "Exactly when and where do we capture an email?"
    )
    assert skill._hlt_source_search_seeds(
        "Exactly when and where do we capture an email?",
        hlt_scoped=False,
    ) == []


def test_hlt_seeded_discovery_opens_source_before_model_research(monkeypatch):
    source_url = (
        "https://github.com/Awhitter/nursing-mastery/blob/"
        + "a" * 40
        + "/lib/jobs/feed.ts#L20-L80"
    )
    search_tool = FakeTool(
        "search_source",
        json.dumps(
            {
                "matches": [
                    {
                        "repo": "Awhitter/nursing-mastery",
                        "commitSha": "a" * 40,
                        "path": "lib/jobs/feed.ts",
                        "line": 40,
                        "authority": 4,
                        "score": 10,
                        "url": source_url,
                    }
                ]
            }
        ),
    )
    read_tool = FakeTool(
        "read_source",
        json.dumps(
            {
                "repo": "Awhitter/nursing-mastery",
                "commitSha": "a" * 40,
                "path": "lib/jobs/feed.ts",
                "url": source_url,
                "content": "export async function getJobs(filters) {}",
            }
        ),
    )

    class NoToolCallLLM:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, messages):
            assert any("high-value source matches" in str(message.content) for message in messages)
            return AIMessage(content="The opened feed contains getJobs.")

    monkeypatch.setattr(
        "gpt_researcher.llm_provider.generic.base.GenericLLMProvider.from_provider",
        lambda *_args, **_kwargs: SimpleNamespace(llm=NoToolCallLLM()),
    )
    cfg = SimpleNamespace(
        strategic_llm_model="test-model",
        strategic_llm_provider="test-provider",
        llm_kwargs={},
    )
    query = (
        "How does Nursing Mastery job search work?\n\n"
        "HLT research scope instructions:\n- Internal source run."
    )

    results = asyncio.run(
        MCPResearchSkill(
            cfg,
            SimpleNamespace(mcp_only=True),
        ).conduct_research_with_tools(query, [search_tool, read_tool])
    )

    assert search_tool.calls == [
        {"query_text": "getJobs getCuratedJobs getRankedJobs job search"}
    ]
    assert read_tool.calls == [
        {
            "repo": "Awhitter/nursing-mastery",
            "path": "lib/jobs/feed.ts",
            "start_line": 10,
            "end_line": 100,
        }
    ]
    assert any("getJobs" in item["body"] for item in results)


def test_hlt_seeded_discovery_runs_for_scoped_parent_and_plain_child(monkeypatch):
    source_url = (
        "https://github.com/Awhitter/nursing-mastery/blob/"
        + "a" * 40
        + "/components/identity/EmailCaptureCard.tsx#L20-L70"
    )
    search_tool = FakeTool(
        "search_source",
        json.dumps(
            {
                "matches": [
                    {
                        "repo": "Awhitter/nursing-mastery",
                        "commitSha": "a" * 40,
                        "path": "components/identity/EmailCaptureCard.tsx",
                        "line": 40,
                        "authority": 4,
                        "score": 10,
                        "url": source_url,
                    }
                ]
            }
        ),
    )
    read_tool = FakeTool(
        "read_source",
        json.dumps(
            {
                "repo": "Awhitter/nursing-mastery",
                "commitSha": "a" * 40,
                "path": "components/identity/EmailCaptureCard.tsx",
                "url": source_url,
                "content": "await fetch('/api/identity', { method: 'POST' })",
            }
        ),
    )

    class NoToolCallLLM:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            return AIMessage(content="The opened component posts the captured email.")

    monkeypatch.setattr(
        "gpt_researcher.llm_provider.generic.base.GenericLLMProvider.from_provider",
        lambda *_args, **_kwargs: SimpleNamespace(llm=NoToolCallLLM()),
    )
    cfg = SimpleNamespace(
        strategic_llm_model="test-model",
        strategic_llm_provider="test-provider",
        llm_kwargs={},
    )
    researcher = SimpleNamespace(
        mcp_only=True,
        query=(
            "Please answer five Nursing Mastery questions.\n\n"
            "HLT research scope instructions:\n- Internal source run."
        ),
        parent_query="",
    )

    asyncio.run(
        MCPResearchSkill(cfg, researcher).conduct_research_with_tools(
            "Exactly when and where do we capture a nurse email?",
            [search_tool, read_tool],
        )
    )

    assert search_tool.calls == [
        {
            "query_text": (
                "QuickStartBridge EmailCaptureCard POST /api/identity email capture"
            )
        }
    ]
    assert read_tool.calls == [
        {
            "repo": "Awhitter/nursing-mastery",
            "path": "components/identity/EmailCaptureCard.tsx",
            "start_line": 10,
            "end_line": 100,
        }
    ]
