"""The plan door: Katailyst's model calls answered on the ChatGPT plans signed in on the server.

These pin what Katailyst relies on: a chat-completions body becomes a Codex Responses request the plan
accepts, an account that runs out hands the call to the next one, every failure is a status code
(so Katailyst can use its Gateway), and the answer comes back in the chat-completions shape.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import time
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


def jwt(exp):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


class FakePool:
    """Hermes' credential pool, as much of it as the door uses (agent/credential_pool.py at 29112bef)."""

    def __init__(self, ids, *, refresh_ok=True):
        self._entries = [SimpleNamespace(id=i, runtime_api_key=f"token-{i}", access_token=None, last_status=None,
                                         last_error_reset_at=None) for i in ids]
        self.resting: list[tuple[str, int]] = []
        self.contexts: list[dict] = []
        self.refreshes: list[str] = []
        self.refresh_ok = refresh_ok

    def entries(self):
        return list(self._entries)

    def select(self):
        rested = {i for i, _ in self.resting}
        return next((e for e in self._entries if e.id not in rested), None)

    def mark_exhausted_and_rotate(self, *, status_code, credential_id, error_context=None, **_):
        self.resting.append((credential_id, status_code))
        self.contexts.append(error_context or {})
        entry = next((e for e in self._entries if e.id == credential_id), None)
        if entry is not None:
            entry.last_status = "exhausted"
            entry.last_error_reset_at = time.time() + 3600
        return self.select()

    def try_refresh_matching(self, *, credential_id, **_):
        self.refreshes.append(credential_id)
        entry = next(e for e in self._entries if e.id == credential_id)
        if not self.refresh_ok:
            return None
        entry.runtime_api_key = f"fresh-{credential_id}"
        return entry


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
    # The effort is passed to Hermes' clamp as asked (gpt-5.6 takes ``max``), never narrowed first.
    assert request["reasoning"] == {"effort": "max"}
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


def test_a_stale_sign_in_is_refreshed_and_the_same_account_asked_again():
    pool = FakePool(["first", "second"])
    seen = []

    def answer(request):
        if request.headers["Authorization"] == "Bearer token-first":
            return httpx.Response(401, json={"error": {"code": "token_expired"}})
        return httpx.Response(200, text=finished("ready"))

    assert make_door(pool, answer, seen).complete(BODY)["choices"][0]["message"]["content"] == "ready"
    assert [r.headers["Authorization"] for r in seen] == ["Bearer token-first", "Bearer fresh-first"]
    assert pool.refreshes == ["first"] and pool.resting == []


def test_a_sign_in_that_cannot_be_refreshed_rests_and_the_next_account_answers():
    pool = FakePool(["first", "second"], refresh_ok=False)
    seen = []

    def answer(request):
        if request.headers["Authorization"] == "Bearer token-first":
            return httpx.Response(401, json={"error": {"code": "token_revoked"}})
        return httpx.Response(200, text=finished("ready"))

    make_door(pool, answer, seen).complete(BODY)
    assert pool.resting == [("first", 401)]
    assert len(seen) == 2


def test_a_sign_in_still_refused_after_its_refresh_is_asked_only_twice():
    pool = FakePool(["only"])
    seen = []
    with pytest.raises(PlanDoorError) as caught:
        make_door(pool, lambda _r: httpx.Response(401, json={"error": {"code": "token_expired"}}), seen).complete(BODY)
    assert caught.value.kind == "plans_busy"
    assert len(seen) == 2 and pool.refreshes == ["only"] and pool.resting == [("only", 401)]


def test_a_limit_rests_the_account_until_the_plan_says_it_resets():
    pool = FakePool(["first", "second"])

    def answer(request):
        if request.headers["Authorization"] == "Bearer token-first":
            return httpx.Response(429, json={"error": {"code": "usage_limit_reached", "message": "limit", "resets_in_seconds": 3600}})
        return httpx.Response(200, text=finished("ready"))

    before = time.time()
    make_door(pool, answer).complete(BODY)
    context = pool.contexts[0]
    assert context["reason"] == "usage_limit_reached" and context["message"] == "limit"
    assert before + 3590 < context["resets_at"] < time.time() + 3610


def test_an_edge_refusal_rests_no_account():
    pool = FakePool(["a", "b"])
    seen = []
    with pytest.raises(PlanDoorError) as caught:
        make_door(pool, lambda _r: httpx.Response(403, text="<html>Just a moment...</html>"), seen).complete(BODY)
    assert caught.value.status == 502
    assert pool.resting == [] and len(seen) == 1


def test_an_account_the_plan_refuses_by_name_rests_and_the_next_one_answers():
    pool = FakePool(["first", "second"])

    def answer(request):
        if request.headers["Authorization"] == "Bearer token-first":
            return httpx.Response(403, json={"detail": "This account is deactivated."})
        return httpx.Response(200, text=finished("ready"))

    assert make_door(pool, answer).complete(BODY)["choices"][0]["message"]["content"] == "ready"
    assert pool.resting == [("first", 403)]


def test_accounts_lists_each_plan_by_position_with_its_state():
    pool = FakePool(["a", "b", "c"])
    now = time.time()
    a, b, c = pool.entries()
    a.access_token = jwt(now + 10 * 3600)
    b.last_status, b.last_error_reset_at = "exhausted", now + 600
    c.last_status = "dead"
    listed = make_door(pool, lambda _r: httpx.Response(200)).accounts()
    assert listed["signedIn"] == 3 and listed["available"] == 1
    assert [item["state"] for item in listed["accounts"]] == ["ready", "resting", "signed_out"]
    assert 9.9 <= listed["accounts"][0]["signInExpiresInHours"] <= 10.0
    assert listed["accounts"][1]["signInExpiresInHours"] is None


def test_the_keeper_refreshes_only_sign_ins_that_run_out_soon():
    pool = FakePool(["soon", "later", "unknown", "gone"])
    now = time.time()
    soon, later, _unknown, gone = pool.entries()
    soon.access_token = jwt(now + 3600)
    later.access_token = jwt(now + 9 * 24 * 3600)
    gone.last_status = "dead"
    counts = make_door(pool, lambda _r: httpx.Response(200)).keep_signed_in()
    assert pool.refreshes == ["soon"]
    assert counts == {"checked": 4, "refreshed": 1, "failed": 1}


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


def test_mixed_tier_pool_unions_models_and_routes_frontier_calls_past_limited_accounts():
    pool = FakePool(["resting-pro", "plus", "pro"])
    pool.mark_exhausted_and_rotate(status_code=429, credential_id="resting-pro")
    seen = []

    def answer(request: httpx.Request):
        auth = request.headers["Authorization"]
        if request.method == "GET":
            if auth == "Bearer token-plus":
                return httpx.Response(200, json={"models": [{"slug": "gpt-6-luna"}, {"slug": "gpt-5.6-terra"}]})
            if auth == "Bearer token-pro":
                return httpx.Response(200, json={"models": [{"slug": "gpt-6.1-sol"}, {"slug": "gpt-6-astra"}, {"slug": "gpt-6-luna"}]})
            raise AssertionError(f"resting account should not be queried: {auth}")
        if auth == "Bearer token-plus":
            return httpx.Response(400, json={"detail": "The 'gpt-6.1-sol' model is not supported."})
        return httpx.Response(200, text=finished("answered by pro"))

    door = make_door(pool, answer, seen)
    assert door.models() == ["gpt-5.6-terra", "gpt-6-astra", "gpt-6-luna", "gpt-6.1-sol"]
    assert [r.headers["Authorization"] for r in seen] == ["Bearer token-plus", "Bearer token-pro"]

    # Cached per-account capability skips the limited account immediately for gpt-6.1-sol without resting it.
    seen.clear()
    result = door.complete({**BODY, "model": "openai/gpt-6.1-sol"})
    assert result["choices"][0]["message"]["content"] == "answered by pro"
    assert [r.headers["Authorization"] for r in seen] == ["Bearer token-pro"]
    assert pool.resting == [("resting-pro", 429)]


def test_complete_rotates_past_404_model_refusal_without_resting_account():
    pool = FakePool(["plus", "pro"])
    seen = []

    def answer(request: httpx.Request):
        if request.headers["Authorization"] == "Bearer token-plus":
            return httpx.Response(400, json={"detail": "The 'gpt-6.1-sol' model is not supported."})
        return httpx.Response(200, text=finished("from pro"))

    door = make_door(pool, answer, seen)
    result = door.complete({**BODY, "model": "openai/gpt-6.1-sol"})
    assert result["choices"][0]["message"]["content"] == "from pro"
    assert [r.headers["Authorization"] for r in seen] == ["Bearer token-plus", "Bearer token-pro"]
    # The limited account was NOT rested, so it can still answer models it supports.
    assert pool.resting == []


def test_models_survives_one_failing_account_and_refreshes_stale_401():
    pool = FakePool(["stale", "broken", "timeout", "html", "empty", "healthy"])
    seen = []

    def answer(request: httpx.Request):
        auth = request.headers["Authorization"]
        if auth == "Bearer token-stale":
            return httpx.Response(401, json={"error": {"code": "token_expired"}})
        if auth == "Bearer fresh-stale":
            return httpx.Response(200, json={"models": [{"slug": "gpt-6-astra"}]})
        if auth == "Bearer token-broken":
            return httpx.Response(502, text="bad gateway")
        if auth == "Bearer token-timeout":
            raise httpx.ReadTimeout("timed out")
        if auth == "Bearer token-html":
            return httpx.Response(200, text="<html>challenge</html>")
        if auth == "Bearer token-empty":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={"models": [{"slug": "gpt-6.1-sol"}]})

    door = make_door(pool, answer, seen)
    assert door.models() == ["gpt-6-astra", "gpt-6.1-sol"]
    assert pool.refreshes == ["stale"]
    # An empty 200 never poisons account capability into skipping that account forever.
    assert door._known_models("empty") is None


def test_models_wraps_around_selected_and_skips_resting_accounts_after_selected():
    pool = FakePool(["early-pro", "plus", "resting-later"])
    pool.mark_exhausted_and_rotate(status_code=429, credential_id="resting-later")
    # Simulate non-index-0 select (e.g. round_robin or deferred refresh) selecting "plus" first.
    pool.select = lambda: pool.entries()[1]
    seen = []

    def answer(request: httpx.Request):
        auth = request.headers["Authorization"]
        if auth == "Bearer token-plus":
            return httpx.Response(200, json={"models": [{"slug": "gpt-6-luna"}]})
        if auth == "Bearer token-early-pro":
            return httpx.Response(200, json={"models": [{"slug": "gpt-6.1-sol"}]})
        raise AssertionError(f"resting account should not be queried: {auth}")

    door = make_door(pool, answer, seen)
    assert door.models() == ["gpt-6-luna", "gpt-6.1-sol"]
    assert [r.headers["Authorization"] for r in seen] == ["Bearer token-plus", "Bearer token-early-pro"]


def test_when_pro_accounts_are_429_and_plus_is_ready_frontier_returns_404_not_429():
    now = [0.0]
    pool = FakePool(["plus", "pro"])

    def answer(request: httpx.Request):
        auth = request.headers["Authorization"]
        if request.method == "GET":
            slugs = [{"slug": "gpt-6-luna"}] if auth == "Bearer token-plus" else [{"slug": "gpt-6.1-sol"}]
            return httpx.Response(200, json={"models": slugs})
        if auth == "Bearer token-pro":
            return httpx.Response(429, json={"error": {"code": "usage_limit_reached"}})
        return httpx.Response(200, text=finished("from plus"))

    door = PlanDoor(
        pool_loader=lambda: pool,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(answer)),
        headers_for=lambda token: {"Authorization": f"Bearer {token}"},
        to_input=plain_input,
        clamp_effort=lambda effort, _slug: effort,
        clock=lambda: now[0],
    )
    assert door.models() == ["gpt-6-luna", "gpt-6.1-sol"]
    # Pro hits 429 while Plus is still ready for gpt-6-luna: return 404 (not 429) so Katailyst does not rest the whole door.
    with pytest.raises(PlanDoorError) as caught:
        door.complete({**BODY, "model": "openai/gpt-6.1-sol"})
    assert caught.value.status == 404
    assert pool.resting == [("pro", 429)]
    # Plus still serves gpt-6-luna immediately.
    assert door.complete({**BODY, "model": "openai/gpt-6-luna"})["choices"][0]["message"]["content"] == "from plus"
    # Per-account capability cache expires after MODELS_CACHE_SECONDS.
    now[0] = door_module.MODELS_CACHE_SECONDS + 1
    assert door._known_models("plus") is None


def test_dead_entry_after_terminal_refresh_is_not_overwritten_as_exhausted():
    pool = FakePool(["revoked", "healthy"])

    def terminal_refresh(*, credential_id, **_):
        entry = next(e for e in pool.entries() if e.id == credential_id)
        entry.last_status = "dead"
        return None

    pool.try_refresh_matching = terminal_refresh

    def answer(request: httpx.Request):
        if request.headers["Authorization"] == "Bearer token-revoked":
            return httpx.Response(401, json={"error": {"code": "token_expired"}})
        return httpx.Response(200, text=finished("from healthy"))

    assert make_door(pool, answer).complete(BODY)["choices"][0]["message"]["content"] == "from healthy"
    assert pool.resting == []
    assert pool.entries()[0].last_status == "dead"


fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")


def _app(door, monkeypatch, token="door-secret"):
    if token is None:
        monkeypatch.delenv(door_module.TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(door_module.TOKEN_ENV, token)
    app = fastapi.FastAPI()
    door_module.install(app, door, keeper=False)
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
    accounts = client.get("/plan/v1/accounts", headers=auth).json()
    assert (accounts["signedIn"], accounts["available"]) == (1, 1)
    assert (accounts["callsRunning"], accounts["callCapacity"], accounts["lastFailure"]) == (0, door_module.CONCURRENT_CALLS, None)


def test_accounts_reports_the_last_miss_by_kind_status_and_time_only(monkeypatch):
    door = make_door(FakePool(["a"]), lambda _r: httpx.Response(429, json={"error": {"message": "secret detail"}}))
    client = _app(door, monkeypatch)
    auth = {"Authorization": "Bearer door-secret"}
    assert client.post("/plan/v1/chat/completions", json=BODY, headers=auth).status_code == 429
    failure = client.get("/plan/v1/accounts", headers=auth).json()["lastFailure"]
    assert set(failure) == {"kind", "status", "at"}
    assert (failure["kind"], failure["status"]) == ("plans_busy", 429)
    assert failure["at"].endswith("Z")


def test_a_busy_plan_is_a_429_the_caller_can_fall_back_on(monkeypatch):
    door = make_door(FakePool(["a"]), lambda _r: httpx.Response(429, json={}))
    answer = _app(door, monkeypatch).post("/plan/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer door-secret"})
    assert answer.status_code == 429
    assert answer.json()["error"]["type"] == "plans_busy"
