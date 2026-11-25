"""No-network tests for Alec's $0/call ChatGPT Plan Door client in GPT Researcher."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.server import hlt_scope_inference as inference
from gpt_researcher.llm_provider import plan_door_client
from gpt_researcher.llm_provider.generic.base import GenericLLMProvider
from gpt_researcher.utils.llm import construct_subtopics, create_chat_completion


@pytest.fixture(autouse=True)
def _clean_plan_door(monkeypatch):
    plan_door_client.reset_plan_door_state()
    monkeypatch.delenv("HLT_PLAN_DOOR_TOKEN", raising=False)
    monkeypatch.delenv("HLT_PLAN_DOOR_URL", raising=False)
    yield
    plan_door_client.reset_plan_door_state()


def _plan_response(
    content: str = "Plan answer",
    *,
    finish_reason: str = "stop",
    served: bool = True,
    plan_model: str | None = "gpt-6.1-sol",
    req_id: str = "chatcmpl-plan-1",
    refusal: str | None = None,
    tool_calls: list | None = None,
) -> httpx.Response:
    message: dict = {"role": "assistant", "content": content}
    if refusal:
        message["refusal"] = refusal
    if tool_calls:
        message["tool_calls"] = tool_calls
    plan_obj: dict = {"served": served}
    if plan_model is not None:
        plan_obj["model"] = plan_model
    body = {
        "id": req_id,
        "model": "gpt-6.1-sol",
        "plan": plan_obj,
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
    }
    return httpx.Response(200, json=body)


def _models_response(models: list[str]) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": [{"id": m, "object": "model"} for m in models]})


def test_plan_door_serves_vercel_openai_chat_and_caches_models(monkeypatch):
    monkeypatch.setenv("HLT_PLAN_DOOR_TOKEN", "plan-secret-token")
    monkeypatch.setenv("HLT_PLAN_DOOR_URL", "https://plans.example/plan/v1")

    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer plan-secret-token"
        if request.url.path.endswith("/models"):
            seen.append(("GET", request.url.path, None))
            return _models_response(["openai/gpt-6.1-sol"])
        payload = json.loads(request.content.decode("utf-8"))
        seen.append(("POST", request.url.path, payload))
        return _plan_response("Researched on $0 plan")

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res = await plan_door_client.try_plan_door_chat(
                [{"role": "user", "content": "Summarize nurse licensing"}],
                model="openai/gpt-6.1-sol",
                client=client,
            )
            res2 = await plan_door_client.try_plan_door_chat(
                [{"role": "user", "content": "Second question"}],
                model="openai/gpt-6.1-sol",
                client=client,
            )
            return res, res2

    res, res2 = asyncio.run(_run())
    assert res == "Researched on $0 plan"
    assert res2 == "Researched on $0 plan"
    assert [entry[0] for entry in seen] == ["GET", "POST", "POST"]
    assert seen[1][2]["model"] == "openai/gpt-6.1-sol"
    assert seen[1][2]["stream"] is False


def test_generic_provider_uses_plan_door_first_and_zeroes_cost_callback(monkeypatch):
    monkeypatch.setenv("HLT_PLAN_DOOR_TOKEN", "plan-secret-token")
    llm = SimpleNamespace(model_name="openai/gpt-6.1-sol", ainvoke=AsyncMock(), astream=AsyncMock())
    provider = GenericLLMProvider(llm, provider="vercel")

    costs: list[float] = []
    with (
        patch("gpt_researcher.llm_provider.plan_door_client.try_plan_door_chat", new=AsyncMock(return_value="Line 1\nLine 2")),
        patch("gpt_researcher.utils.llm.get_llm", return_value=provider),
    ):
        result = asyncio.run(create_chat_completion(
            messages=[{"role": "user", "content": "Draft section"}],
            model="openai/gpt-6.1-sol",
            llm_provider="vercel",
            cost_callback=costs.append,
        ))
    assert result == "Line 1\nLine 2"
    assert provider.last_served_by == "plan"
    assert costs == [0.0]
    llm.ainvoke.assert_not_called()


def test_validate_gateway_options_fires_before_plan_door(monkeypatch):
    monkeypatch.setenv("HLT_PLAN_DOOR_TOKEN", "plan-secret-token")
    llm = SimpleNamespace(model_name="openai/gpt-6.1-sol", ainvoke=AsyncMock(), astream=AsyncMock())
    provider = GenericLLMProvider(llm, provider="vercel")

    with patch("gpt_researcher.llm_provider.plan_door_client.try_plan_door_chat", new=AsyncMock()) as mock_plan:
        with pytest.raises(ValueError, match="cannot be overridden"):
            asyncio.run(provider.get_chat_response(
                [{"role": "user", "content": "Hi"}],
                False,
                extra_headers={"Authorization": "Bearer evil"},
            ))
    mock_plan.assert_not_called()


def test_generic_provider_respects_per_call_model_override(monkeypatch):
    monkeypatch.setenv("HLT_PLAN_DOOR_TOKEN", "plan-secret-token")
    llm = SimpleNamespace(
        model_name="openai/gpt-6.1-sol",
        ainvoke=AsyncMock(return_value=SimpleNamespace(content="gw-claude")),
        astream=AsyncMock(),
    )
    provider = GenericLLMProvider(llm, provider="vercel")
    with patch("gpt_researcher.llm_provider.plan_door_client.try_plan_door_chat", new=AsyncMock(return_value=None)) as mock_plan:
        out = asyncio.run(provider.get_chat_response(
            [{"role": "user", "content": "Hi"}],
            False,
            model="anthropic/claude-sonnet-5.6",
        ))
    assert out == "gw-claude"
    assert mock_plan.call_args.kwargs["model"] == "anthropic/claude-sonnet-5.6"


def test_generic_provider_stream_emits_paragraphs_on_plan_and_falls_back_cleanly(monkeypatch):
    monkeypatch.setenv("HLT_PLAN_DOOR_TOKEN", "plan-secret-token")
    ws_messages: list[dict] = []

    class _FakeWS:
        async def send_json(self, payload: dict):
            ws_messages.append(payload)

    llm = SimpleNamespace(model_name="openai/gpt-6.1-sol", ainvoke=AsyncMock(), astream=AsyncMock())
    provider = GenericLLMProvider(llm, provider="vercel", verbose=False)

    with patch("gpt_researcher.llm_provider.plan_door_client.try_plan_door_chat", new=AsyncMock(return_value="Para one\nPara two")):
        result = asyncio.run(provider.stream_response([{"role": "user", "content": "Hi"}], websocket=_FakeWS()))
    assert result == "Para one\nPara two"
    assert provider.last_served_by == "plan"
    assert ws_messages == [
        {"type": "report", "output": "Para one\n"},
        {"type": "report", "output": "Para two"},
    ]
    llm.astream.assert_not_called()

    ws_messages.clear()

    async def _fake_astream(_messages, **_kwargs):
        yield SimpleNamespace(content="Gateway ")
        yield SimpleNamespace(content="fallback\n")

    llm.astream = _fake_astream
    with patch("gpt_researcher.llm_provider.plan_door_client.try_plan_door_chat", new=AsyncMock(return_value=None)):
        fallback = asyncio.run(provider.stream_response([{"role": "user", "content": "Hi"}], websocket=_FakeWS()))
    assert fallback == "Gateway fallback\n"
    assert provider.last_served_by == "gateway"
    assert ws_messages == [{"type": "report", "output": "Gateway fallback\n"}]


def test_plan_door_refuses_zdr_nested_overrides_promoted_attrs_urls_and_invalid_responses(monkeypatch):
    monkeypatch.setenv("HLT_PLAN_DOOR_TOKEN", "plan-secret-token")
    called = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called += 1
        if request.url.path.endswith("/models"):
            return _models_response(["openai/gpt-6.1-sol"])
        return _plan_response("should not accept")

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            # Non-HTTPS and query/fragment URLs are refused before any network call.
            for bad_url in ("http://evil.example/plan/v1", "https://plans.example/plan/v1?x=1", "https://plans.example/plan/v1#frag"):
                monkeypatch.setenv("HLT_PLAN_DOOR_URL", bad_url)
                assert await plan_door_client.try_plan_door_chat(
                    [{"role": "user", "content": "Hi"}], model="openai/gpt-6.1-sol", client=client,
                ) is None

            monkeypatch.setenv("HLT_PLAN_DOOR_URL", "https://plans.example/plan/v1")
            # Non-OpenAI model never calls the plan door.
            assert await plan_door_client.try_plan_door_chat(
                [{"role": "user", "content": "Hi"}], model="anthropic/claude-sonnet-5.6", client=client,
            ) is None

            # ZDR, snake_case provider_options, empty gateway.only, and nested disallowed options.
            restricted_cases = [
                {"extra_body": {"providerOptions": {"gateway": {"zeroDataRetention": True}}}},
                {"extra_body": {"provider_options": {"gateway": {"zeroDataRetention": True}}}},
                {"extra_body": {"providerOptions": {"gateway": {"only": []}}}},
                {"extra_body": {"providerOptions": {"gateway": {"only": ["anthropic"]}}}},
                {"extra_body": {"tools": [{"type": "function", "function": {"name": "f"}}]}},
                {"model_kwargs": {"stop": ["END"]}},
                {"modalities": "audio"},
            ]
            for kwargs in restricted_cases:
                assert await plan_door_client.try_plan_door_chat(
                    [{"role": "user", "content": "Hi"}], model="openai/gpt-6.1-sol", kwargs=kwargs, client=client,
                ) is None

            # Shallow kwargs must not overwrite ZDR on llm.model_kwargs.
            zdr_llm = SimpleNamespace(model_kwargs={"providerOptions": {"gateway": {"zeroDataRetention": True}}})
            assert await plan_door_client.try_plan_door_chat(
                [{"role": "user", "content": "Hi"}],
                model="openai/gpt-6.1-sol",
                kwargs={"providerOptions": {"gateway": {"tags": ["research"]}}},
                llm=zdr_llm,
                client=client,
            ) is None

            # Promoted ChatOpenAI attributes (stop, n, logprobs) refuse Plan Door.
            for bad_llm in (
                SimpleNamespace(n=2),
                SimpleNamespace(stop=["END"]),
                SimpleNamespace(logprobs=True),
                SimpleNamespace(use_responses_api=True),
            ):
                assert await plan_door_client.try_plan_door_chat(
                    [{"role": "user", "content": "Hi"}], model="openai/gpt-6.1-sol", llm=bad_llm, client=client,
                ) is None

    asyncio.run(_run())
    assert called == 0

    for bad in [
        _plan_response("x", req_id="gen_123"),
        _plan_response("x", served=False),
        _plan_response("x", plan_model=""),
        _plan_response("x", finish_reason="length"),
        _plan_response("x", finish_reason="tool_calls"),
        _plan_response("x", tool_calls=[{"id": "t1"}]),
        _plan_response("x", refusal="cannot comply"),
    ]:
        assert plan_door_client._extract_valid_plan_content(bad) is None


def test_plan_door_rests_on_429_and_negatively_caches_models_failures(monkeypatch):
    monkeypatch.setenv("HLT_PLAN_DOOR_TOKEN", "plan-secret-token")
    monkeypatch.setenv("HLT_PLAN_DOOR_URL", "https://plans.example/plan/v1")

    # 1. Negative caching when /models returns 503 prevents repeated 3s stalls.
    model_gets = 0

    def down_handler(request: httpx.Request) -> httpx.Response:
        nonlocal model_gets
        model_gets += 1
        return httpx.Response(503, json={"error": {"type": "plan_door_off"}})

    async def _run_down():
        async with httpx.AsyncClient(transport=httpx.MockTransport(down_handler)) as client:
            assert await plan_door_client.try_plan_door_chat([{"role": "user", "content": "A"}], model="openai/gpt-6.1-sol", client=client) is None
            assert await plan_door_client.try_plan_door_chat([{"role": "user", "content": "B"}], model="openai/gpt-6.1-sol", client=client) is None

    asyncio.run(_run_down())
    assert model_gets == 1

    # 2. HTTP 429 rests for 60s.
    plan_door_client.reset_plan_door_state()
    posts = 0

    def limit_handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path.endswith("/models"):
            return _models_response(["openai/gpt-6.1-sol"])
        posts += 1
        return httpx.Response(429, json={"error": {"type": "plans_resting"}})

    async def _run_limit():
        async with httpx.AsyncClient(transport=httpx.MockTransport(limit_handler)) as client:
            assert await plan_door_client.try_plan_door_chat([{"role": "user", "content": "First"}], model="openai/gpt-6.1-sol", client=client) is None
            assert await plan_door_client.try_plan_door_chat([{"role": "user", "content": "Second"}], model="openai/gpt-6.1-sol", client=client) is None

    asyncio.run(_run_limit())
    assert posts == 1


def test_plan_door_rejects_oversized_response_and_redirects(monkeypatch):
    monkeypatch.setenv("HLT_PLAN_DOOR_TOKEN", "plan-secret-token")
    monkeypatch.setenv("HLT_PLAN_DOOR_URL", "https://plans.example/plan/v1")
    monkeypatch.setattr(plan_door_client, "MAX_RESPONSE_BYTES", 64)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _models_response(["openai/gpt-6.1-sol"])
        return _plan_response("x" * 200)

    async def _run_big():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await plan_door_client.try_plan_door_chat(
                [{"role": "user", "content": "Hi"}], model="openai/gpt-6.1-sol", client=client,
            )

    assert asyncio.run(_run_big()) is None

    plan_door_client.reset_plan_door_state()

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://evil.example/steal"})

    async def _run_redirect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect_handler), follow_redirects=True) as client:
            return await plan_door_client.try_plan_door_chat(
                [{"role": "user", "content": "Hi"}], model="openai/gpt-6.1-sol", client=client,
            )

    assert asyncio.run(_run_redirect()) is None


def test_construct_subtopics_uses_plan_door_and_validates_gateway_options(monkeypatch):
    monkeypatch.setenv("HLT_PLAN_DOOR_TOKEN", "plan-secret-token")
    cfg = SimpleNamespace(
        smart_llm_provider="vercel",
        smart_llm_model="openai/gpt-6.1-sol",
        llm_kwargs={},
        temperature=0.4,
        smart_token_limit=2000,
        max_subtopics=3,
    )
    fake_provider = GenericLLMProvider(SimpleNamespace(model_name="openai/gpt-6.1-sol"), provider="vercel")
    with (
        patch("gpt_researcher.utils.llm.get_llm", return_value=fake_provider),
        patch(
            "gpt_researcher.llm_provider.plan_door_client.try_plan_door_chat",
            new=AsyncMock(return_value='{"subtopics": [{"task": "Pay transparency"}]}'),
        ),
    ):
        result = asyncio.run(construct_subtopics("Nurse pay", "Data", cfg, subtopics=[]))
    assert [s.task for s in result.subtopics] == ["Pay transparency"]


def test_scope_inference_uses_plan_door_and_falls_back_to_openai(monkeypatch):
    monkeypatch.setenv("FAST_LLM", "vercel:openai/gpt-6.1-sol")
    monkeypatch.setenv("HLT_PLAN_DOOR_TOKEN", "plan-secret-token")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with patch(
        "gpt_researcher.llm_provider.plan_door_client.try_plan_door_chat_sync",
        return_value='{"scopes": ["metrics"]}',
    ) as mock_plan:
        approved = inference._llm_tiebreak("Are leads improving?", ["metrics"])
    assert approved == {"metrics"}
    assert mock_plan.call_args.kwargs["model"] == "openai/gpt-6.1-sol"
    assert mock_plan.call_args.kwargs["kwargs"]["reasoning_effort"] == "low"

    # When Plan Door returns None, OpenAI fallback uses gpt-4o-mini (not gpt-6.1-sol with temp=0).
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured_payload: dict = {}

    class _FakeResp:
        def read(self):
            return json.dumps({"choices": [{"message": {"content": '{"scopes": ["metrics"]}'}}]}).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def _fake_urlopen(req, timeout):
        captured_payload.update(json.loads(req.data.decode("utf-8")))
        return _FakeResp()

    with (
        patch("gpt_researcher.llm_provider.plan_door_client.try_plan_door_chat_sync", return_value=None),
        patch.object(inference.urllib.request, "urlopen", _fake_urlopen),
    ):
        fallback = inference._llm_tiebreak("Are leads improving?", ["metrics"])
    assert fallback == {"metrics"}
    assert captured_payload["model"] == "gpt-4o-mini"
