"""The plan door: OpenAI chat calls answered on the ChatGPT plans signed in here.

Owner, 15 Nov 2026 ("All agents, through one plan door"): every Katailyst agent tries Alec's ChatGPT
plans first, then the Vercel AI Gateway, then OpenRouter. Katailyst asks this door for a model call in
the OpenAI chat-completions shape it already speaks. The door asks the Codex Responses endpoint
(chatgpt.com/backend-api/codex) with one of the plan sign-ins Hermes keeps in its credential pool,
and answers in the chat-completions shape. When an account runs out it is marked and the next one is
asked, so the plans are used one after another (the pool's fill-first order), as Alec uses them.

Any answer other than 200 tells Katailyst to use its Gateway for that call. The door never spends
money: a plan sign-in is capped at the plan's price while the account's credit switches stay off.

What it does not do: hold a conversation, run tools or keep anything. It answers one model call, and
the message text never reaches a log. A stream request is answered when the plan has finished, as one
stream of chunks, so a failure is always a status code Katailyst can fall back on, never a cut stream.
"""

import base64
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import httpx

logger = logging.getLogger("plan_door")

TOKEN_ENV = "HLT_PLAN_DOOR_TOKEN"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
PROVIDER = "openai-codex"
# Katailyst names OpenAI models the Gateway way; the plan knows the bare slug.
MODEL_PREFIX = "openai/"
DEFAULT_INSTRUCTIONS = "You are a helpful assistant."
CALL_TIMEOUT_SECONDS = 300.0
MODELS_CACHE_SECONDS = 600.0
# Plan calls in flight at once. Each holds a worker thread for up to minutes, so they get their own
# lane and never queue the server's health check behind them; a call beyond it goes to the Gateway.
CONCURRENT_CALLS = 8
# A limit or an exhausted plan rests that account and the next one is asked.
REST_STATUSES = frozenset({402, 429})
# A sign-in whose access token runs out within this long is refreshed by the keeper (``keep_signed_in``).
REFRESH_AHEAD_SECONDS = 3 * 24 * 60 * 60


class PlanDoorError(Exception):
    """A call the door cannot serve. ``status`` is what Katailyst sees; any of them means use the Gateway."""

    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message

    def body(self) -> dict[str, Any]:
        return {"error": {"type": self.kind, "message": self.message}}


def door_token(env: Mapping[str, str] | None = None) -> str:
    return ((env if env is not None else os.environ).get(TOKEN_ENV) or "").strip()


def authorized(authorization: str | None, env: Mapping[str, str] | None = None) -> bool:
    expected = door_token(env)
    supplied = ""
    if isinstance(authorization, str) and authorization.startswith("Bearer "):
        supplied = authorization[7:].strip()
    return bool(expected) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def plan_slug(model: Any) -> str:
    """The plan's own name for a Katailyst model id: ``openai/gpt-5.6-sol`` -> ``gpt-5.6-sol``."""
    if not isinstance(model, str) or not model.strip():
        raise PlanDoorError(400, "invalid_request", "model is required")
    value = model.strip()
    if "/" in value:
        if not value.startswith(MODEL_PREFIX):
            raise PlanDoorError(404, "model_not_on_plan", "only OpenAI models run on a ChatGPT plan")
        value = value[len(MODEL_PREFIX):]
    if not value or "/" in value:
        raise PlanDoorError(404, "model_not_on_plan", "only OpenAI models run on a ChatGPT plan")
    return value


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping) and part.get("type") in {"text", "input_text"}
        )
    return "" if content is None else str(content)


def _effort(body: Mapping[str, Any]) -> str | None:
    raw = body.get("reasoning_effort")
    reasoning = body.get("reasoning")
    if raw is None and isinstance(reasoning, Mapping):
        if reasoning.get("enabled") is False:
            return None
        raw = reasoning.get("effort")
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    return value or None


def _tool_choice(choice: Any) -> Any:
    if isinstance(choice, str) and choice in {"auto", "none", "required"}:
        return choice
    if isinstance(choice, Mapping):
        function = choice.get("function")
        name = function.get("name") if isinstance(function, Mapping) else choice.get("name")
        if isinstance(name, str) and name:
            return {"type": "function", "name": name}
    return None


def _text_format(response_format: Any) -> dict[str, Any] | None:
    if not isinstance(response_format, Mapping):
        return None
    kind = response_format.get("type")
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema":
        spec = response_format.get("json_schema")
        if not isinstance(spec, Mapping) or not isinstance(spec.get("schema"), Mapping):
            raise PlanDoorError(400, "invalid_request", "json_schema needs a schema")
        out: dict[str, Any] = {
            "type": "json_schema",
            "name": str(spec.get("name") or "response"),
            "schema": spec["schema"],
        }
        if isinstance(spec.get("strict"), bool):
            out["strict"] = spec["strict"]
        if isinstance(spec.get("description"), str):
            out["description"] = spec["description"]
        return out
    return None


def responses_request(
    body: Mapping[str, Any],
    *,
    to_input: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    clamp_effort: Callable[[str, str], str | None] = lambda effort, _slug: effort,
) -> dict[str, Any]:
    """The Codex Responses request for one chat-completions body.

    System and developer messages become ``instructions``; the rest go through Hermes' own
    chat-to-Responses converter (``to_input``), the one its agents use, so tool calls and tool
    results replay the same way. The plan refuses ``temperature``, ``top_p`` and token limits, so
    they are left out rather than sent and refused.
    """
    slug = plan_slug(body.get("model"))
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise PlanDoorError(400, "invalid_request", "messages are required")
    instructions: list[str] = []
    replay: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise PlanDoorError(400, "invalid_request", "each message must be an object")
        if message.get("role") in {"system", "developer"}:
            text = _text_of(message.get("content")).strip()
            if text:
                instructions.append(text)
        else:
            replay.append(dict(message))
    request: dict[str, Any] = {
        "model": slug,
        "instructions": "\n\n".join(instructions) or DEFAULT_INSTRUCTIONS,
        "input": to_input(replay) or [{"role": "user", "content": ""}],
        "store": False,
        "stream": True,
    }
    tools = []
    for tool in body.get("tools") or []:
        function = tool.get("function") if isinstance(tool, Mapping) else None
        if not isinstance(function, Mapping) or not function.get("name"):
            continue
        converted: dict[str, Any] = {
            "type": "function",
            "name": function["name"],
            "description": function.get("description") or "",
            "parameters": function.get("parameters") or {"type": "object", "properties": {}},
        }
        if isinstance(function.get("strict"), bool):
            converted["strict"] = function["strict"]
        tools.append(converted)
    if tools:
        request["tools"] = tools
        choice = _tool_choice(body.get("tool_choice"))
        if choice is not None:
            request["tool_choice"] = choice
        if isinstance(body.get("parallel_tool_calls"), bool):
            request["parallel_tool_calls"] = body["parallel_tool_calls"]
    text_format = _text_format(body.get("response_format"))
    if text_format:
        request["text"] = {"format": text_format}
    effort = _effort(body)
    if effort:
        clamped = clamp_effort(effort, slug)
        if clamped:
            request["reasoning"] = {"effort": clamped}
    return request


def _sse_events(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    data: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].strip())
            continue
        if line.strip() == "" and data:
            payload = "\n".join(data)
            data = []
            if payload == "[DONE]":
                return
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event
    if data:
        try:
            event = json.loads("\n".join(data))
        except ValueError:
            return
        if isinstance(event, dict):
            yield event


def collect_response(events: Iterator[dict[str, Any]]) -> dict[str, Any]:
    """The finished response from a Codex event stream.

    Output items are taken from ``response.output_item.done`` as they finish: the closing
    ``response.completed`` event has been seen with ``output: null`` (Hermes, May 2026), so it is
    only used for usage and status, and for its output when no item event came.
    """
    items: list[dict[str, Any]] = []
    final: dict[str, Any] | None = None
    for event in events:
        kind = event.get("type")
        if kind == "response.output_item.done" and isinstance(event.get("item"), dict):
            items.append(event["item"])
        elif kind in {"response.completed", "response.incomplete"} and isinstance(event.get("response"), dict):
            final = event["response"]
        elif kind in {"response.failed", "error"}:
            failed = event.get("response") if isinstance(event.get("response"), dict) else event
            error = failed.get("error") if isinstance(failed.get("error"), dict) else failed
            code = str(error.get("code") or error.get("type") or "plan_failed")
            status = 429 if "limit" in code or "quota" in code else 502
            raise PlanDoorError(status, code, "the plan did not finish this call")
    if final is None:
        raise PlanDoorError(502, "plan_incomplete", "the plan's answer ended early")
    if not items and isinstance(final.get("output"), list):
        items = [item for item in final["output"] if isinstance(item, dict)]
    return {**final, "output": items}


def chat_completion(response: Mapping[str, Any], requested_model: str) -> dict[str, Any]:
    """The chat-completions answer for a finished Responses answer."""
    text: list[str] = []
    calls: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if isinstance(part, Mapping) and part.get("type") in {"output_text", "text"}:
                    text.append(str(part.get("text") or ""))
        elif kind == "function_call":
            calls.append({
                "id": str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {"name": str(item.get("name") or ""), "arguments": str(item.get("arguments") or "{}")},
            })
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text) if text else None}
    if calls:
        message["tool_calls"] = calls
    incomplete = response.get("status") == "incomplete"
    finish = "tool_calls" if calls else ("length" if incomplete else "stop")
    usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") if isinstance(usage.get("input_tokens_details"), Mapping) else None
    reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens") if isinstance(usage.get("output_tokens_details"), Mapping) else None
    out_usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(usage.get("total_tokens") or prompt + completion),
    }
    if isinstance(cached, int):
        out_usage["prompt_tokens_details"] = {"cached_tokens": cached}
    if isinstance(reasoning, int):
        out_usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {
        "id": f"chatcmpl-plan-{response.get('id') or uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(response.get("created_at") or time.time()),
        "model": requested_model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": out_usage,
        # Katailyst reads this to record the call as served on a plan, not by the Gateway.
        "plan": {"served": True, "model": requested_model},
    }


def stream_chunks(completion: Mapping[str, Any]) -> Iterator[str]:
    """One finished answer as chat-completions stream chunks, ending with usage and ``[DONE]``."""
    base = {"id": completion["id"], "object": "chat.completion.chunk", "created": completion["created"], "model": completion["model"]}
    choice = completion["choices"][0]
    message = choice["message"]
    delta: dict[str, Any] = {"role": "assistant"}
    if message.get("content"):
        delta["content"] = message["content"]
    if message.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
    yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]})}\n\n"
    last = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}],
            "usage": completion["usage"], "plan": completion["plan"]}
    yield f"data: {json.dumps(last)}\n\n"
    yield "data: [DONE]\n\n"


def _codex_headers(token: str) -> dict[str, str]:
    from agent.codex_headers import codex_cloudflare_headers

    return {
        **codex_cloudflare_headers(token, base_url=CODEX_BASE_URL),
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


def _hermes_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from agent.codex_responses_adapter import _chat_messages_to_responses_input

    return _chat_messages_to_responses_input(messages, native_compaction_eligible=False)


def _hermes_clamp(effort: str, slug: str) -> str | None:
    from agent.reasoning_effort import clamp_effort, codex_supported_efforts

    return clamp_effort(effort, codex_supported_efforts(slug))


def _load_pool():
    from agent.credential_pool import load_pool

    return load_pool(PROVIDER)


class PlanDoor:
    """Asks the signed-in plans, one account after another, for one model call."""

    def __init__(
        self,
        *,
        pool_loader: Callable[[], Any] = _load_pool,
        client_factory: Callable[[], httpx.Client] | None = None,
        headers_for: Callable[[str], dict[str, str]] = _codex_headers,
        to_input: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] = _hermes_input,
        clamp_effort: Callable[[str, str], str | None] = _hermes_clamp,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._pool_loader = pool_loader
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=httpx.Timeout(CALL_TIMEOUT_SECONDS, connect=10.0)))
        self._headers_for = headers_for
        self._to_input = to_input
        self._clamp_effort = clamp_effort
        self._clock = clock
        self._models: tuple[float, list[str]] | None = None

    def complete(self, body: Mapping[str, Any]) -> dict[str, Any]:
        request = responses_request(body, to_input=self._to_input, clamp_effort=self._clamp_effort)
        requested = str(body.get("model"))
        pool = self._pool_loader()
        tried: set[str] = set()
        refreshed: set[str] = set()
        again: str | None = None
        with self._client_factory() as client:
            while True:
                entry = pool.select() if pool is not None else None
                if entry is None or (entry.id in tried and entry.id != again):
                    break
                again = None
                tried.add(entry.id)
                with client.stream("POST", f"{CODEX_BASE_URL}/responses", headers=self._headers_for(entry.runtime_api_key), json=request) as answer:
                    if answer.status_code == 401:
                        answer.read()
                        # A stale access token: refresh it once and ask the same account again.
                        if entry.id not in refreshed and pool.try_refresh_matching(credential_id=entry.id) is not None:
                            logger.info("plan door: account %s's sign-in was refreshed", len(tried))
                            refreshed.add(entry.id)
                            again = entry.id
                            continue
                        pool.mark_exhausted_and_rotate(status_code=401, credential_id=entry.id, error_context=_error_context(answer))
                        continue
                    if answer.status_code in REST_STATUSES or (answer.status_code == 403 and _account_refusal(answer)):
                        logger.info("plan door: account %s answered %s, trying the next one", len(tried), answer.status_code)
                        pool.mark_exhausted_and_rotate(status_code=answer.status_code, credential_id=entry.id, error_context=_error_context(answer))
                        continue
                    if answer.status_code == 404 or (answer.status_code == 400 and self._model_refused(answer)):
                        raise PlanDoorError(404, "model_not_on_plan", f"{request['model']} is not on the plan")
                    if answer.status_code != 200:
                        # A refusal not about one account (an edge challenge, a server fault) rests nobody.
                        answer.read()
                        raise PlanDoorError(502, "plan_error", f"the plan answered {answer.status_code}")
                    try:
                        finished = collect_response(_sse_events(answer.iter_lines()))
                    except PlanDoorError as error:
                        if error.status != 429:
                            raise
                        logger.info("plan door: account %s ran out mid-call, trying the next one", len(tried))
                        pool.mark_exhausted_and_rotate(status_code=429, credential_id=entry.id, error_context={"reason": error.kind, "message": error.message})
                        continue
                return chat_completion(finished, requested)
        raise PlanDoorError(429, "plans_busy", "every signed-in plan is resting; use the Gateway")

    @staticmethod
    def _model_refused(answer: httpx.Response) -> bool:
        text = answer.read().decode("utf-8", "replace").lower()
        return "model" in text and ("not supported" in text or "does not exist" in text or "unsupported" in text)

    def models(self) -> list[str]:
        """The models the signed-in plan offers, from Codex's own list, kept ten minutes."""
        now = self._clock()
        if self._models and now - self._models[0] < MODELS_CACHE_SECONDS:
            return self._models[1]
        pool = self._pool_loader()
        entry = pool.select() if pool is not None else None
        if entry is None:
            raise PlanDoorError(429, "plans_busy", "no signed-in plan is available")
        with self._client_factory() as client:
            answer = client.get(f"{CODEX_BASE_URL}/models", params={"client_version": "1.0.0"}, headers=self._headers_for(entry.runtime_api_key))
        if answer.status_code != 200:
            raise PlanDoorError(502, "plan_error", f"the plan's model list answered {answer.status_code}")
        data = answer.json()
        slugs = sorted({
            item["slug"].strip()
            for item in (data.get("models") if isinstance(data, dict) else None) or []
            if isinstance(item, dict) and isinstance(item.get("slug"), str) and item["slug"].strip()
        })
        self._models = (now, slugs)
        return slugs

    def accounts(self) -> dict[str, Any]:
        """How many plans are signed in, how many can take a call now, and each one's state.

        Accounts are listed by position only: a sign-in's label can be an email address.
        """
        pool = self._pool_loader()
        entries = pool.entries() if pool is not None else []
        now = time.time()
        listed = []
        for index, entry in enumerate(entries, start=1):
            expires = _token_expiry(entry.access_token)
            listed.append({
                "account": index,
                "state": _state(entry, now),
                "signInExpiresInHours": None if expires is None else round((expires - now) / 3600, 1),
            })
        return {
            "signedIn": len(entries),
            "available": sum(1 for item in listed if item["state"] == "ready"),
            "accounts": listed,
        }

    def keep_signed_in(self) -> dict[str, int]:
        """Refresh each sign-in whose access token runs out soon, so an account Alec does not use for a
        while is still signed in when its turn comes. Fill-first use leaves the later accounts idle
        for days, and Hermes only refreshes an account when it is about to use it."""
        pool = self._pool_loader()
        entries = pool.entries() if pool is not None else []
        now = time.time()
        counts = {"checked": len(entries), "refreshed": 0, "failed": 0}
        for index, entry in enumerate(entries, start=1):
            if _state(entry, now) == "signed_out":
                counts["failed"] += 1
                continue
            expires = _token_expiry(entry.access_token)
            if expires is None or expires - now > REFRESH_AHEAD_SECONDS:
                continue
            try:
                fresh = pool.try_refresh_matching(credential_id=entry.id)
            except Exception as error:  # noqa: BLE001 — one account's failure must not stop the others
                logger.warning("plan door: account %s could not be refreshed (%s)", index, type(error).__name__)
                fresh = None
            if fresh is None:
                counts["failed"] += 1
                logger.warning("plan door: account %s's sign-in could not be refreshed; it needs signing in again", index)
            else:
                counts["refreshed"] += 1
        return counts


def _state(entry: Any, now: float) -> str:
    status = getattr(entry, "last_status", None)
    if status == "dead":
        return "signed_out"
    if status == "exhausted":
        reset = getattr(entry, "last_error_reset_at", None)
        if not isinstance(reset, (int, float)) or reset > now:
            return "resting"
    return "ready"


def _token_expiry(token: Any) -> float | None:
    """The ``exp`` of a JWT access token, read without verifying it (it is our own sign-in)."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, (int, float)) else None


def _error_body(answer: httpx.Response) -> Any:
    try:
        return json.loads(answer.read() or b"null")
    except (ValueError, httpx.HTTPError):
        return None


def _account_refusal(answer: httpx.Response) -> bool:
    """A 403 rests the account only when the plan says, in its own JSON, why this account was refused.

    An edge challenge or a throttle on this server's address comes back as a page, not as the plan's
    error object, and resting every account for it would take the plans away from everyone for an hour.
    """
    body = _error_body(answer)
    error = body.get("error") if isinstance(body, dict) else None
    detail = body.get("detail") if isinstance(body, dict) else None
    return isinstance(error, dict) or isinstance(detail, (dict, str))


def _error_context(answer: httpx.Response) -> dict[str, Any]:
    """What Hermes' pool needs to rest an account until its plan resets, not for a flat hour."""
    body = _error_body(answer)
    payload = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else body
    context: dict[str, Any] = {}
    if isinstance(payload, dict):
        reason = payload.get("code") or payload.get("type")
        if isinstance(reason, str) and reason.strip():
            context["reason"] = reason.strip()
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            context["message"] = message.strip()
        if payload.get("resets_at") not in (None, ""):
            context["resets_at"] = payload["resets_at"]
        elif isinstance(payload.get("resets_in_seconds"), (int, float)):
            context["resets_at"] = time.time() + float(payload["resets_in_seconds"])
    retry_after = answer.headers.get("retry-after")
    if retry_after and "resets_at" not in context:
        try:
            context["resets_at"] = time.time() + float(retry_after)
        except ValueError:
            pass
    return context


KEEPER_INTERVAL_SECONDS = 6 * 60 * 60


def _start_keeper(door: PlanDoor) -> None:
    """Every six hours, refresh the sign-ins that run out soon (``PlanDoor.keep_signed_in``)."""
    import threading

    def run() -> None:
        while True:
            try:
                counts = door.keep_signed_in()
                logger.info("plan door keeper: %s", counts)
            except Exception as error:  # noqa: BLE001 — the keeper never takes the server down
                logger.warning("plan door keeper: skipped this round (%s)", type(error).__name__)
            time.sleep(KEEPER_INTERVAL_SECONDS)

    threading.Thread(target=run, name="plan-door-keeper", daemon=True).start()


def install(app: Any, door: PlanDoor | None = None, *, keeper: bool | None = None) -> PlanDoor:
    """Add the door's three routes to the server's FastAPI app, and start the sign-in keeper.

    The keeper runs only where the door is switched on (its key is set) unless told otherwise.
    """
    if keeper is None:
        keeper = bool(door_token())
    import anyio
    from fastapi import Header, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse, StreamingResponse

    door = door or PlanDoor()
    lane = anyio.CapacityLimiter(CONCURRENT_CALLS)
    if keeper:
        _start_keeper(door)

    def refusal(error: PlanDoorError) -> JSONResponse:
        return JSONResponse(error.body(), status_code=error.status)

    def closed(authorization: str | None) -> JSONResponse | None:
        if not door_token():
            return refusal(PlanDoorError(503, "plan_door_off", "the plan door is not configured"))
        if not authorized(authorization):
            return refusal(PlanDoorError(401, "unauthorized", "unauthorized"))
        return None

    @app.post("/plan/v1/chat/completions")
    async def plan_chat_completions(request: Request, authorization: str | None = Header(default=None)):
        if (shut := closed(authorization)) is not None:
            return shut
        try:
            body = await request.json()
        except ValueError:
            return refusal(PlanDoorError(400, "invalid_request", "the body is not JSON"))
        if not isinstance(body, dict):
            return refusal(PlanDoorError(400, "invalid_request", "the body must be an object"))
        if lane.available_tokens < 1:
            return refusal(PlanDoorError(429, "plans_busy", "the plans are answering as many calls as they take at once"))
        started = time.monotonic()
        try:
            completion = await anyio.to_thread.run_sync(door.complete, body, limiter=lane)
        except PlanDoorError as error:
            logger.info("plan door: %s (%s) after %.1fs", error.kind, error.status, time.monotonic() - started)
            return refusal(error)
        except httpx.HTTPError as error:
            logger.warning("plan door: the plan could not be reached (%s)", type(error).__name__)
            return refusal(PlanDoorError(502, "plan_unreachable", "the plan could not be reached"))
        logger.info("plan door: %s answered in %.1fs", completion["model"], time.monotonic() - started)
        if body.get("stream") is True:
            return StreamingResponse(stream_chunks(completion), media_type="text/event-stream")
        return JSONResponse(completion)

    @app.get("/plan/v1/models")
    async def plan_models(authorization: str | None = Header(default=None)):
        if (shut := closed(authorization)) is not None:
            return shut
        try:
            slugs = await run_in_threadpool(door.models)
        except PlanDoorError as error:
            return refusal(error)
        except httpx.HTTPError:
            return refusal(PlanDoorError(502, "plan_unreachable", "the plan could not be reached"))
        return JSONResponse({"object": "list", "data": [{"id": f"{MODEL_PREFIX}{slug}", "object": "model"} for slug in slugs]})

    @app.get("/plan/v1/accounts")
    async def plan_accounts(authorization: str | None = Header(default=None)):
        if (shut := closed(authorization)) is not None:
            return shut
        return JSONResponse(await run_in_threadpool(door.accounts))

    return door
