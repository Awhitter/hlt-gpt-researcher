"""First-attempt routing for HLT research chat calls through Alec's $0/call ChatGPT Plan Door.

Mirrors Katailyst's ``gateway-plan-door.ts`` contract for ``hlt-research-api`` and
``hlt-research-mcp`` (both internal HLT services):
- Fail-open to Vercel AI Gateway on any refusal, timeout, HTTP error, or malformed response.
- Strictly ``openai/*`` chat completions only (embeddings and non-OpenAI models never touch it).
- Strict HTTPS URL check (loopback ``http://`` allowed for tests only), ``follow_redirects=False``,
  and zero secret logging.
- Caches ``GET /models`` for 10 minutes per (base_url, token), negatively caches failures for 60s,
  and rests for 60s on HTTP 429/402.
- Enforces streaming byte caps (1 MiB ``/models``, 4 MiB chat) before buffering and validates
  ``plan.served === True``, non-empty ``plan.model``, non-``gen_`` id, and non-truncated/non-declined
  choices before accepting an answer.
"""

from __future__ import annotations

from contextlib import AsyncExitStack, ExitStack
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

PLAN_DOOR_TOKEN_ENV = "HLT_PLAN_DOOR_TOKEN"
PLAN_DOOR_URL_ENV = "HLT_PLAN_DOOR_URL"
DEFAULT_PLAN_DOOR_URL = "https://hlt-agent-plans.onrender.com/plan/v1"

MODELS_CACHE_SECONDS = 600.0
CLOSED_CACHE_SECONDS = 60.0
REST_AFTER_LIMIT_SECONDS = 60.0
MODELS_TIMEOUT_SECONDS = 3.0
DEFAULT_CALL_TIMEOUT_SECONDS = 90.0
MAX_RESPONSE_BYTES = 4_000_000
MAX_MODELS_BYTES = 1_048_576

_DISALLOWED_OPTION_KEYS = frozenset({
    "tools",
    "functions",
    "tool_choice",
    "function_call",
    "audio",
    "web_search_options",
    "search_parameters",
    "plugins",
    "provider_options",
    "stop",
    "logprobs",
    "top_logprobs",
    "prediction",
    "logit_bias",
})

_PROMOTED_LLM_DISALLOWED_ATTRS = (
    "stop",
    "logprobs",
    "top_logprobs",
    "logit_bias",
    "use_responses_api",
)

_LANGCHAIN_ROLE_MAP = {
    "human": "user",
    "user": "user",
    "ai": "assistant",
    "assistant": "assistant",
    "system": "system",
    "developer": "developer",
}

_state_lock = threading.Lock()
_models_cache: tuple[float, str, str, frozenset[str]] | None = None
_resting_until: float = 0.0


def reset_plan_door_state() -> None:
    """Clear in-process model cache and 429 rest window (for tests)."""
    global _models_cache, _resting_until
    with _state_lock:
        _models_cache = None
        _resting_until = 0.0


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _auth_headers(token: str, *, json_body: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _validated_base_url(raw: str | None) -> str | None:
    stripped = (raw or "").strip()
    candidate = (stripped or DEFAULT_PLAN_DOOR_URL).rstrip("/")
    parsed = urlparse(candidate)
    try:
        _ = parsed.port
    except ValueError:
        return None
    if parsed.username or parsed.password or not parsed.hostname:
        return None
    if parsed.params or parsed.query or parsed.fragment:
        return None
    host = parsed.hostname.lower()
    if parsed.scheme == "https":
        return candidate
    if parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}:
        return candidate
    return None


def _door_config(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    source = env if env is not None else os.environ
    token = (source.get(PLAN_DOOR_TOKEN_ENV) or "").strip()
    if not token:
        return None
    base_url = _validated_base_url(source.get(PLAN_DOOR_URL_ENV))
    if not base_url:
        logger.warning("Plan door skipped: %s must be a clean https URL", PLAN_DOOR_URL_ENV)
        return None
    return base_url, token


def normalize_plan_model(model: Any, *, bare_openai_ok: bool = False) -> str | None:
    if not isinstance(model, str):
        return None
    cleaned = model.strip()
    if not cleaned:
        return None
    if cleaned.startswith("openai/"):
        slug = cleaned[len("openai/"):]
        return cleaned if slug and "/" not in slug else None
    if bare_openai_ok and "/" not in cleaned:
        return f"openai/{cleaned}"
    return None


def _is_resting(now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    with _state_lock:
        return current < _resting_until


def _note_resting(seconds: float = REST_AFTER_LIMIT_SECONDS) -> None:
    global _resting_until
    with _state_lock:
        _resting_until = max(_resting_until, time.monotonic() + seconds)


def _cached_models(base_url: str, token: str) -> frozenset[str] | None:
    now = time.monotonic()
    fp = _token_fingerprint(token)
    with _state_lock:
        if _models_cache is None:
            return None
        expires_at, cached_url, cached_fp, models = _models_cache
        if now < expires_at and cached_url == base_url and cached_fp == fp:
            return models
    return None


def _store_models(base_url: str, token: str, models: frozenset[str], *, ttl: float = MODELS_CACHE_SECONDS) -> None:
    global _models_cache
    with _state_lock:
        _models_cache = (
            time.monotonic() + ttl,
            base_url,
            _token_fingerprint(token),
            models,
        )


def _parse_models_payload(payload: Any) -> frozenset[str]:
    if not isinstance(payload, Mapping):
        return frozenset()
    data = payload.get("data")
    if not isinstance(data, list):
        return frozenset()
    out: set[str] = set()
    for entry in data:
        if isinstance(entry, Mapping):
            model_id = normalize_plan_model(entry.get("id"))
            if model_id:
                out.add(model_id)
    return frozenset(out)


def _response_format_supported(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, Mapping):
        return False
    kind = value.get("type")
    if kind == "json_object":
        return True
    if kind == "json_schema":
        spec = value.get("json_schema")
        return isinstance(spec, Mapping) and isinstance(spec.get("schema"), Mapping)
    return False


def _mapping_allows_plan_door(options: Mapping[str, Any] | None, *, depth: int = 0) -> bool:
    """Recursively verify that an options container asks for nothing the Plan Door refuses or drops."""
    if not isinstance(options, Mapping):
        return True
    if depth > 6:
        return False

    if options.get("zeroDataRetention") is True or options.get("zero_data_retention") is True or options.get("zdr") is True:
        return False
    if options.get("n") not in (None, 1):
        return False
    if "modalities" in options and options["modalities"] is not None:
        modalities = options["modalities"]
        if not isinstance(modalities, Sequence) or isinstance(modalities, (str, bytes)):
            return False
        if any(item != "text" for item in modalities):
            return False

    for key in _DISALLOWED_OPTION_KEYS:
        if options.get(key) is not None:
            return False
    if not _response_format_supported(options.get("response_format")):
        return False

    provider = options.get("provider")
    if isinstance(provider, Mapping):
        if provider.get("zdr") is True or provider.get("zeroDataRetention") is True or provider.get("zero_data_retention") is True:
            return False

    gateway = options.get("gateway")
    if isinstance(gateway, Mapping):
        if gateway.get("zeroDataRetention") is True or gateway.get("zero_data_retention") is True:
            return False
        if "only" in gateway and gateway["only"] is not None:
            only = gateway["only"]
            if not isinstance(only, Sequence) or isinstance(only, (str, bytes)):
                return False
            allowed = {str(item).strip().lower() for item in only if str(item).strip()}
            if "openai" not in allowed:
                return False

    for nested_key in ("extra_body", "providerOptions", "model_kwargs"):
        nested = options.get(nested_key)
        if nested is not None:
            if not isinstance(nested, Mapping):
                return False
            if not _mapping_allows_plan_door(nested, depth=depth + 1):
                return False
    return True


def _options_allow_plan_door(kwargs: Mapping[str, Any] | None, llm: Any = None) -> bool:
    if kwargs is not None and not _mapping_allows_plan_door(kwargs):
        return False
    if llm is not None:
        if getattr(llm, "n", None) not in (None, 1):
            return False
        for attr in _PROMOTED_LLM_DISALLOWED_ATTRS:
            val = getattr(llm, attr, None)
            if val not in (None, False, "", [], {}):
                return False
        for attr in ("model_kwargs", "extra_body"):
            val = getattr(llm, attr, None)
            if isinstance(val, Mapping) and not _mapping_allows_plan_door(val):
                return False
    return True


def _normalize_content(content: Any) -> str | list[dict[str, Any]] | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        normalized: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, Mapping):
                return None
            kind = part.get("type")
            if kind == "text" and isinstance(part.get("text"), str):
                normalized.append({"type": "text", "text": part["text"]})
                continue
            if kind == "image_url":
                img = part.get("image_url")
                url = img.get("url") if isinstance(img, Mapping) else None
                if isinstance(url, str) and (
                    url.strip().lower().startswith("https://")
                    or url.strip().lower().startswith("data:image/")
                ):
                    normalized.append({"type": "image_url", "image_url": {"url": url.strip()}})
                    continue
            return None
        return normalized
    return None


def _normalize_messages(messages: Any) -> list[dict[str, Any]] | None:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
        return None
    out: list[dict[str, Any]] = []
    for item in messages:
        if isinstance(item, Mapping):
            if (
                item.get("audio") is not None
                or item.get("tool_calls") is not None
                or item.get("function_call") is not None
            ):
                return None
            role = item.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                return None
            content = _normalize_content(item.get("content"))
            if content is None:
                return None
            out.append({"role": role, "content": content})
            continue
        msg_type = getattr(item, "type", None)
        mapped_role = _LANGCHAIN_ROLE_MAP.get(str(msg_type).lower()) if isinstance(msg_type, str) else None
        if not mapped_role:
            return None
        if getattr(item, "tool_calls", None):
            return None
        additional = getattr(item, "additional_kwargs", None)
        if isinstance(additional, Mapping) and any(
            additional.get(k) for k in ("tool_calls", "function_call", "audio")
        ):
            return None
        content = _normalize_content(getattr(item, "content", None))
        if content is None:
            return None
        out.append({"role": mapped_role, "content": content})
    return out


def _extract_response_format(kwargs: Mapping[str, Any] | None, llm: Any) -> Any:
    for container in (
        kwargs,
        kwargs.get("model_kwargs") if isinstance(kwargs, Mapping) else None,
        kwargs.get("extra_body") if isinstance(kwargs, Mapping) else None,
        getattr(llm, "model_kwargs", None) if llm is not None else None,
        getattr(llm, "extra_body", None) if llm is not None else None,
    ):
        if isinstance(container, Mapping) and container.get("response_format") is not None:
            return container["response_format"]
    return None


def _extract_reasoning_effort(kwargs: Mapping[str, Any] | None, llm: Any) -> str | None:
    candidates: list[Any] = []
    if isinstance(kwargs, Mapping):
        candidates.append(kwargs.get("reasoning_effort"))
        r = kwargs.get("reasoning")
        if isinstance(r, Mapping) and r.get("enabled") is not False:
            candidates.append(r.get("effort"))
    if llm is not None:
        candidates.append(getattr(llm, "reasoning_effort", None))
        r = getattr(llm, "reasoning", None)
        if isinstance(r, Mapping) and r.get("enabled") is not False:
            candidates.append(r.get("effort"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return None


def _build_payload(
    model_id: str,
    messages: list[dict[str, Any]],
    kwargs: Mapping[str, Any] | None = None,
    llm: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "stream": False,
    }
    response_format = _extract_response_format(kwargs, llm)
    if response_format is not None:
        payload["response_format"] = response_format
    effort = _extract_reasoning_effort(kwargs, llm)
    if effort:
        payload["reasoning_effort"] = effort
    return payload


def _prepare_plan_request(
    messages: Any,
    *,
    model: Any,
    kwargs: Mapping[str, Any] | None = None,
    llm: Any = None,
    bare_openai_ok: bool = False,
) -> tuple[str, str, str, dict[str, Any]] | None:
    config = _door_config()
    if config is None or _is_resting():
        return None
    model_id = normalize_plan_model(model, bare_openai_ok=bare_openai_ok)
    if not model_id or not _options_allow_plan_door(kwargs, llm=llm):
        return None
    normalized_messages = _normalize_messages(messages)
    if not normalized_messages:
        return None
    base_url, token = config
    payload = _build_payload(model_id, normalized_messages, kwargs=kwargs, llm=llm)
    return base_url, token, model_id, payload


def _exceeds_content_length(response: httpx.Response, limit: int) -> bool:
    header = response.headers.get("content-length")
    if not header:
        return False
    try:
        return int(header) > limit
    except ValueError:
        return False


def _is_json_response(response: httpx.Response) -> bool:
    return "application/json" in (response.headers.get("content-type") or "").lower()


async def _read_bounded_async(response: httpx.Response, limit: int) -> bytes | None:
    if _exceeds_content_length(response, limit):
        return None
    chunks = bytearray()
    async for chunk in response.aiter_bytes():
        chunks.extend(chunk)
        if len(chunks) > limit:
            return None
    return bytes(chunks)


def _read_bounded_sync(response: httpx.Response, limit: int) -> bytes | None:
    if _exceeds_content_length(response, limit):
        return None
    chunks = bytearray()
    for chunk in response.iter_bytes():
        chunks.extend(chunk)
        if len(chunks) > limit:
            return None
    return bytes(chunks)


def _decode_valid_plan_content(status_code: int, raw: bytes | None) -> str | None:
    if status_code in {402, 429}:
        _note_resting()
        return None
    if status_code != 200 or raw is None:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, Mapping):
        return None
    plan = body.get("plan")
    if not isinstance(plan, Mapping) or plan.get("served") is not True:
        return None
    plan_model = plan.get("model")
    if not isinstance(plan_model, str) or not plan_model.strip():
        return None
    req_id = body.get("id")
    if not isinstance(req_id, str) or not req_id.strip() or req_id.lower().startswith("gen_"):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return None
    if choice.get("finish_reason") in {"content_filter", "length", "tool_calls"}:
        return None
    message = choice.get("message")
    if not isinstance(message, Mapping):
        return None
    if message.get("tool_calls"):
        return None
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return None
    return content


def _extract_valid_plan_content(response: httpx.Response) -> str | None:
    """Validate an already-buffered ``httpx.Response`` (used by unit tests)."""
    if response.status_code in {402, 429}:
        _note_resting()
        return None
    if response.status_code != 200 or not _is_json_response(response):
        return None
    raw = response.content
    if len(raw) > MAX_RESPONSE_BYTES:
        return None
    return _decode_valid_plan_content(response.status_code, raw)


def _handle_models_bytes(base_url: str, token: str, status_code: int, is_json: bool, raw: bytes | None) -> frozenset[str] | None:
    if status_code in {402, 429}:
        _note_resting()
        return None
    if status_code != 200 or not is_json or raw is None:
        _store_models(base_url, token, frozenset(), ttl=CLOSED_CACHE_SECONDS)
        return None
    try:
        models = _parse_models_payload(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        _store_models(base_url, token, frozenset(), ttl=CLOSED_CACHE_SECONDS)
        return None
    _store_models(base_url, token, models, ttl=MODELS_CACHE_SECONDS if models else CLOSED_CACHE_SECONDS)
    return models


async def _fetch_models_async(
    base_url: str,
    token: str,
    client: httpx.AsyncClient,
    *,
    timeout: float = MODELS_TIMEOUT_SECONDS,
) -> frozenset[str] | None:
    cached = _cached_models(base_url, token)
    if cached is not None:
        return cached
    try:
        async with client.stream(
            "GET",
            f"{base_url}/models",
            headers=_auth_headers(token),
            timeout=timeout,
            follow_redirects=False,
        ) as response:
            is_json = _is_json_response(response)
            raw = await _read_bounded_async(response, MAX_MODELS_BYTES) if response.status_code == 200 and is_json else None
            return _handle_models_bytes(base_url, token, response.status_code, is_json, raw)
    except Exception as exc:  # noqa: BLE001 — fail open to Gateway; never echo headers
        _store_models(base_url, token, frozenset(), ttl=CLOSED_CACHE_SECONDS)
        logger.warning("Plan door /models lookup failed (%s); using Gateway", type(exc).__name__)
        return None


def _fetch_models_sync(
    base_url: str,
    token: str,
    client: httpx.Client,
    *,
    timeout: float = MODELS_TIMEOUT_SECONDS,
) -> frozenset[str] | None:
    cached = _cached_models(base_url, token)
    if cached is not None:
        return cached
    try:
        with client.stream(
            "GET",
            f"{base_url}/models",
            headers=_auth_headers(token),
            timeout=timeout,
            follow_redirects=False,
        ) as response:
            is_json = _is_json_response(response)
            raw = _read_bounded_sync(response, MAX_MODELS_BYTES) if response.status_code == 200 and is_json else None
            return _handle_models_bytes(base_url, token, response.status_code, is_json, raw)
    except Exception as exc:  # noqa: BLE001 — fail open to Gateway; never echo headers
        _store_models(base_url, token, frozenset(), ttl=CLOSED_CACHE_SECONDS)
        logger.warning("Plan door /models lookup failed (%s); using Gateway", type(exc).__name__)
        return None


async def try_plan_door_chat(
    messages: Any,
    *,
    model: Any,
    kwargs: Mapping[str, Any] | None = None,
    llm: Any = None,
    timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    bare_openai_ok: bool = False,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Attempt one chat completion on Alec's $0/call ChatGPT Plan Door.

    Returns the validated assistant text on success, or ``None`` when unconfigured,
    ineligible, resting, or declined so the caller falls through cleanly to Gateway.
    """
    prepared = _prepare_plan_request(
        messages, model=model, kwargs=kwargs, llm=llm, bare_openai_ok=bare_openai_ok
    )
    if prepared is None:
        return None
    base_url, token, model_id, payload = prepared
    deadline = time.monotonic() + timeout_seconds

    async with AsyncExitStack() as stack:
        active_client = client if client is not None else await stack.enter_async_context(
            httpx.AsyncClient(follow_redirects=False)
        )
        try:
            models_timeout = min(MODELS_TIMEOUT_SECONDS, max(0.1, deadline - time.monotonic()))
            offered = await _fetch_models_async(base_url, token, active_client, timeout=models_timeout)
            if not offered or model_id not in offered:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0.1:
                return None
            async with active_client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=_auth_headers(token, json_body=True),
                json=payload,
                timeout=remaining,
                follow_redirects=False,
            ) as response:
                raw = (
                    await _read_bounded_async(response, MAX_RESPONSE_BYTES)
                    if response.status_code == 200 and _is_json_response(response)
                    else None
                )
                content = _decode_valid_plan_content(response.status_code, raw)
                if content is None:
                    logger.info(
                        "Plan door did not answer for %s (status=%s); falling back to Gateway",
                        model_id,
                        response.status_code,
                    )
                return content
        except Exception as exc:  # noqa: BLE001 — fail open without logging secrets
            logger.warning("Plan door chat failed (%s); falling back to Gateway", type(exc).__name__)
            return None


def try_plan_door_chat_sync(
    messages: Any,
    *,
    model: Any,
    kwargs: Mapping[str, Any] | None = None,
    timeout_seconds: float = 6.0,
    bare_openai_ok: bool = False,
    client: httpx.Client | None = None,
) -> str | None:
    """Synchronous sibling of ``try_plan_door_chat`` with one shared deadline."""
    prepared = _prepare_plan_request(
        messages, model=model, kwargs=kwargs, bare_openai_ok=bare_openai_ok
    )
    if prepared is None:
        return None
    base_url, token, model_id, payload = prepared
    deadline = time.monotonic() + timeout_seconds

    with ExitStack() as stack:
        active_client = client if client is not None else stack.enter_context(
            httpx.Client(follow_redirects=False)
        )
        try:
            models_timeout = min(MODELS_TIMEOUT_SECONDS, max(0.1, deadline - time.monotonic()))
            offered = _fetch_models_sync(base_url, token, active_client, timeout=models_timeout)
            if not offered or model_id not in offered:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0.1:
                return None
            with active_client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=_auth_headers(token, json_body=True),
                json=payload,
                timeout=remaining,
                follow_redirects=False,
            ) as response:
                raw = (
                    _read_bounded_sync(response, MAX_RESPONSE_BYTES)
                    if response.status_code == 200 and _is_json_response(response)
                    else None
                )
                content = _decode_valid_plan_content(response.status_code, raw)
                if content is None:
                    logger.info(
                        "Plan door sync did not answer for %s (status=%s); falling back",
                        model_id,
                        response.status_code,
                    )
                return content
        except Exception as exc:  # noqa: BLE001 — fail open without logging secrets
            logger.warning("Plan door sync chat failed (%s); falling back", type(exc).__name__)
            return None
