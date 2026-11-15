"""The plan door: Katailyst's model calls answered on the ChatGPT plans signed in on the server.

These pin what Katailyst relies on: a chat-completions body becomes a Codex Responses request the plan
accepts, an account that runs out hands the call to the next one, every failure is a status code
(so Katailyst can use its Gateway), and the answer comes back in the chat-completions shape.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "agent"


def _load():
    spec = importlib.util.spec_from_file_location("plan_door_under_test", SERVICE_DIR / "plan_door.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


door_module = _load()
PlanDoor = door_module.PlanDoor
PlanDoorError = door_module.PlanDoorError


def plain_input(messages):
    return [{"role": m["role"], "content": m.get("content")} for m in messages]


class FakePool:
    def __init__(self, ids):
        self.entries = [SimpleNamespace(id=i, runtime_api_key=f"token-{i}") for i in ids]
        self.resting: list[tuple[str, int]] = []

    def select(self):
        rested = {i for i, _ in self.resting}
        return next((e for e in self.entries if e.id not in rested), None)

    def mark_exhausted_and_rotate(self, *, status_code, credential_id, **_):
        self.resting.append((credential_id, status_code))
        return self.select()

    def readiness_counts(self):
        return {"profile_count": len(self.entries), "selectable_count": len(self.entries) - len(self.resting)}


def sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def finished(text="ready", *, output_on_completed=False, calls=()):
    items = []
    if text:
        items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    for call in calls:
        items.append({"type": "function_call", "call_id": call[0], "name": call[1], "arguments": call[2]})
    response = {
        "id": "resp_1", "created_at": 1700000000, "status": "completed",
        "output": items if output_on_completed else None,
        "usage": {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17,
                  "input_tokens_details": {"cached_tokens": 4}, "output_tokens_details": {"reasoning_tokens": 2}},
    }
    events = [] if output_on_completed else [{"type": "response.output_item.done", "item": item} for item in items]
    return sse(*events, {"type": "response.completed", "response": response})


def make_door(pool, handler, seen=None):
    def record(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        return handler(request)

    return PlanDoor(
        pool_loader=lambda: pool,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(record)),
        headers_for=lambda token: {"Authorization": f"Bearer {token}"},
        to_input=plain_input,
        clamp_effort=lambda effort, _slug: effort,
        clock=lambda: 0.0,
    )


BODY = {
    "model": "openai/gpt-5.6-sol",
    "messages": [
        {"role": "system", "content": "You are Julius."},
        {"role": "user", "content": "Say ready."},
    ],
}


def test_request_moves_system_text_to_instructions_and_drops_what_the_plan_refuses():
    request = door_module.responses_request(
        {
            **BODY,
            "temperature": 0.2,
            "max_tokens": 900,
            "top_p": 0.9,
            "reasoning_effort": "max",
            "tools": [{"type": "function", "function": {"name": "search", "description": "find", "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "function", "function": {"name": "search"}},
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object"}, "strict": True}},
        },
        to_input=plain_input,
    )
    assert request["model"] == "gpt-5.6-sol"
    assert request["instructions"] == "You are Julius."
    assert request["input"] == [{"role": "user", "content": "Say ready."}]
    assert request["store"] is False and request["stream"] is True
    assert request["tools"] == [{"type": "function", "name": "search", "description": "find", "parameters": {"type": "object"}}]
    assert request["tool_choice"] == {"type": "function", "name": "search"}
    assert request["text"] == {"format": {"type": "json_schema", "name": "answer", "schema": {"type": "object"}, "strict": True}}
    assert request["reasoning"] == {"effort": "xhigh"}
    for refused in ("temperature", "max_tokens", "max_output_tokens", "top_p"):
        assert refused not in request


def test_a_model_that_is_not_openai_never_reaches_the_plan():
    seen = []
    door = make_door(FakePool(["a"]), lambda _r: httpx.Response(200, text=finished()), seen)
    with pytest.raises(PlanDoorError) as caught:
        door.complete({**BODY, "model": "anthropic/claude-opus-5.5"})
    assert caught.value.status == 404
    assert seen == []


def test_an_account_that_ran_out_hands_the_call_to_the_next_one():
    pool = FakePool(["first", "second"])
    seen = []

    def answer(request):
        if request.headers["Authorization"] == "Bearer token-first":
            return httpx.Response(429, json={"error": {"code": "usage_limit_reached"}})
        return httpx.Response(200, text=finished("ready"))

    completion = make_door(pool, answer, seen).complete(BODY)
    assert [r.headers["Authorization"] for r in seen] == ["Bearer token-first", "Bearer token-second"]
    assert pool.resting == [("first", 429)]
    assert completion["model"] == "openai/gpt-5.6-sol"
    assert completion["choices"][0]["message"] == {"role": "assistant", "content": "ready"}
    assert completion["choices"][0]["finish_reason"] == "stop"
    assert completion["usage"] == {
        "prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17,
        "prompt_tokens_details": {"cached_tokens": 4}, "completion_tokens_details": {"reasoning_tokens": 2},
    }
    assert completion["plan"] == {"served": True, "model": "openai/gpt-5.6-sol"}
    sent = json.loads(seen[-1].content)
    assert sent["model"] == "gpt-5.6-sol" and sent["instructions"] == "You are Julius."


def test_a_limit_reported_inside_the_stream_also_moves_to_the_next_account():
    pool = FakePool(["first", "second"])

    def answer(request):
        if request.headers["Authorization"] == "Bearer token-first":
            return httpx.Response(200, text=sse({"type": "response.failed", "response": {"error": {"code": "rate_limit_exceeded"}}}))
        return httpx.Response(200, text=finished("ready"))

    assert make_door(pool, answer).complete(BODY)["choices"][0]["message"]["content"] == "ready"
    assert pool.resting == [("first", 429)]


def test_when_every_plan_is_resting_the_answer_is_a_status_katailyst_falls_back_on():
    pool = FakePool(["a", "b", "c"])
    seen = []
    with pytest.raises(PlanDoorError) as caught:
        make_door(pool, lambda _r: httpx.Response(429, json={}), seen).complete(BODY)
    assert caught.value.status == 429 and caught.value.kind == "plans_busy"
    assert len(seen) == 3
    assert [status for _, status in pool.resting] == [429, 429, 429]


def test_a_model_the_plan_does_not_offer_is_a_404_not_a_rotation():
    pool = FakePool(["a", "b"])
    answer = lambda _r: httpx.Response(400, json={"detail": "The 'gpt-9' model is not supported when using Codex with a ChatGPT account."})
    with pytest.raises(PlanDoorError) as caught:
        make_door(pool, answer).complete({**BODY, "model": "openai/gpt-9"})
    assert caught.value.status == 404
    assert pool.resting == []


def test_tool_calls_come_back_as_chat_tool_calls():
    calls = [("call_abc", "search", '{"q":"icu"}')]
    completion = make_door(FakePool(["a"]), lambda _r: httpx.Response(200, text=finished("", calls=calls))).complete(BODY)
    choice = completion["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [{"id": "call_abc", "type": "function", "function": {"name": "search", "arguments": '{"q":"icu"}'}}]


def test_output_on_the_closing_event_is_used_when_no_item_events_came():
    completion = make_door(FakePool(["a"]), lambda _r: httpx.Response(200, text=finished("late", output_on_completed=True))).complete(BODY)
    assert completion["choices"][0]["message"]["content"] == "late"


def test_a_stream_that_ends_without_finishing_is_an_error_not_an_empty_answer():
    with pytest.raises(PlanDoorError) as caught:
        make_door(FakePool(["a"]), lambda _r: httpx.Response(200, text=sse({"type": "response.created"}))).complete(BODY)
    assert caught.value.status == 502


def test_a_stream_request_gets_chunks_with_usage_and_done():
    completion = door_module.chat_completion(
        {"id": "r", "created_at": 1, "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
         "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}]},
        "openai/gpt-5.6-sol",
    )
    chunks = list(door_module.stream_chunks(completion))
    assert chunks[-1] == "data: [DONE]\n\n"
    first = json.loads(chunks[0][6:])
    last = json.loads(chunks[1][6:])
    assert first["choices"][0]["delta"] == {"role": "assistant", "content": "hi"}
    assert last["choices"][0]["finish_reason"] == "stop"
    assert last["usage"]["total_tokens"] == 2
    assert last["plan"]["served"] is True


def test_the_model_list_comes_from_the_plan_and_is_kept():
    seen = []
    door = make_door(FakePool(["a"]), lambda _r: httpx.Response(200, json={"models": [{"slug": "gpt-5.6-sol"}, {"slug": "gpt-5.6-luna"}]}), seen)
    assert door.models() == ["gpt-5.6-luna", "gpt-5.6-sol"]
    assert door.models() == ["gpt-5.6-luna", "gpt-5.6-sol"]
    assert len(seen) == 1


fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")


def _app(door, monkeypatch, token="door-secret"):
    if token is None:
        monkeypatch.delenv(door_module.TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(door_module.TOKEN_ENV, token)
    app = fastapi.FastAPI()
    door_module.install(app, door)
    return testclient.TestClient(app)


def test_the_door_is_shut_without_its_key_and_to_a_wrong_key(monkeypatch):
    door = make_door(FakePool(["a"]), lambda _r: httpx.Response(200, text=finished()))
    assert _app(door, monkeypatch, token=None).post("/plan/v1/chat/completions", json=BODY).status_code == 503
    client = _app(door, monkeypatch)
    assert client.post("/plan/v1/chat/completions", json=BODY).status_code == 401
    assert client.post("/plan/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_the_door_answers_plain_and_streamed_calls(monkeypatch):
    door = make_door(FakePool(["a"]), lambda _r: httpx.Response(200, text=finished("ready")))
    client = _app(door, monkeypatch)
    auth = {"Authorization": "Bearer door-secret"}
    plain = client.post("/plan/v1/chat/completions", json=BODY, headers=auth)
    assert plain.status_code == 200
    assert plain.json()["choices"][0]["message"]["content"] == "ready"
    streamed = client.post("/plan/v1/chat/completions", json={**BODY, "stream": True}, headers=auth)
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert streamed.text.endswith("data: [DONE]\n\n")
    assert client.get("/plan/v1/accounts", headers=auth).json() == {"signedIn": 1, "available": 1}


def test_a_busy_plan_is_a_429_the_caller_can_fall_back_on(monkeypatch):
    door = make_door(FakePool(["a"]), lambda _r: httpx.Response(429, json={}))
    answer = _app(door, monkeypatch).post("/plan/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer door-secret"})
    assert answer.status_code == 429
    assert answer.json()["error"]["type"] == "plans_busy"
