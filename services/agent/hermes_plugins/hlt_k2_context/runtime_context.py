"""One bounded Katailyst2 context draw for a Hermes mission.

This module is deliberately self-contained inside the user plugin directory:
Hermes loads user plugins from its durable home and must not depend on the
wrapper process's import path. It uses streamable HTTP directly, exactly like
the boot readiness probe. Current K2 runs the model-judged Well asynchronously,
so this hook starts the durable draw and hands its exact get handle to the model
without waiting. Compact registry search remains the fallback for an incomplete
durable Well tool surface.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping
from typing import Any

MCP_PROTOCOL_VERSION = "2025-03-26"
MAX_CONTEXT_CHARS = 8_000
MAX_CONTEXT_BLOCKS = 12
MISSION_CONTEXT_TIMEOUT_SECONDS = 4.0
WELL_START_NAMES = ("katailyst.well.start", "katailyst_well_start")
WELL_GET_NAMES = ("katailyst.well.get", "katailyst_well_get")
WELL_SYNC_NAMES = ("katailyst.well", "katailyst_well")
REGISTRY_SEARCH_NAMES = ("registry.search", "registry_search")

_CACHE_LOCK = threading.Lock()
_TOOL_CACHE: dict[tuple[str, str], dict[str, str]] = {}
_TRIVIAL_MISSIONS = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "thanks",
        "thank you",
        "ok",
        "okay",
        "got it",
        "cool",
        "great",
        "yes",
        "no",
        "bye",
    }
)


def is_substantive_mission(value: str) -> bool:
    """Skip social acknowledgements, not short real questions."""
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return bool(normalized) and normalized not in _TRIVIAL_MISSIONS


def _decode(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    if text.startswith("{"):
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    for line in text.splitlines():
        if line.startswith("data:"):
            value = json.loads(line[5:].strip())
            return value if isinstance(value, dict) else {}
    raise ValueError("MCP response contained no JSON or SSE data event")


def _post(
    url: str,
    token: str,
    payload: dict[str, Any],
    *,
    session_id: str = "",
    timeout: float = 15.0,
) -> tuple[dict[str, Any], str, dict[str, str]]:
    import urllib.request

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        response_headers = {
            key.lower(): value for key, value in response.headers.items()
        }
    return (
        _decode(raw),
        response_headers.get("mcp-session-id", session_id),
        response_headers,
    )


def _tool_data(result: Mapping[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if not isinstance(content, list):
        return {}
    for item in content:
        if not isinstance(item, Mapping) or item.get("type") != "text":
            continue
        try:
            decoded = json.loads(str(item.get("text") or "{}"))
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return {}


def _cache_key(url: str, token: str) -> tuple[str, str]:
    return url, hashlib.sha256(token.encode()).hexdigest()


def _evict_tool_surface(url: str, token: str) -> None:
    with _CACHE_LOCK:
        _TOOL_CACHE.pop(_cache_key(url, token), None)


def _first_name(names: list[str], candidates: tuple[str, ...]) -> str:
    return next((name for name in names if name in candidates), "")


def _tool_surface(
    url: str,
    token: str,
    *,
    session_id: str,
    rpc: Any,
) -> dict[str, str]:
    key = _cache_key(url, token)
    with _CACHE_LOCK:
        cached = _TOOL_CACHE.get(key)
    if cached:
        return dict(cached)
    listed, _, _ = rpc("tools/list", {}, session_id)
    if listed.get("error"):
        raise RuntimeError(str(listed["error"]))
    names = [
        str(tool.get("name") or "")
        for tool in ((listed.get("result") or {}).get("tools") or [])
        if isinstance(tool, Mapping)
    ]
    surface = {
        "well_start": _first_name(names, WELL_START_NAMES),
        "well_get": _first_name(names, WELL_GET_NAMES),
        "well_sync": _first_name(names, WELL_SYNC_NAMES),
        "registry_search": _first_name(names, REGISTRY_SEARCH_NAMES),
    }
    if not (
        (surface["well_start"] and surface["well_get"])
        or surface["well_sync"]
        or surface["registry_search"]
    ):
        raise RuntimeError("Katailyst context tools are not in this token's surface")
    with _CACHE_LOCK:
        _TOOL_CACHE[key] = dict(surface)
    return surface


def _block_lines(data: Mapping[str, Any]) -> tuple[list[str], int]:
    lines: list[str] = []
    count = 0
    # The synchronous compatibility tool calls these groups ``dives``; the
    # durable start/get contract calls the same shape ``angles``.
    dives = data.get("dives")
    if not isinstance(dives, list):
        dives = data.get("angles")
    if not isinstance(dives, list):
        return lines, count
    for dive in dives:
        if not isinstance(dive, Mapping):
            continue
        facet = str(dive.get("facet") or "useful context").strip()
        if dive.get("abstained") is True:
            lines.append(f"- {facet}: no strong registry block")
            continue
        rows: list[str] = []
        blocks = dive.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if count >= MAX_CONTEXT_BLOCKS or not isinstance(block, Mapping):
                break
            typed_ref = str(block.get("typedRef") or "").strip()
            if not typed_ref:
                block_type = str(block.get("type") or "").strip()
                bare_ref = str(block.get("ref") or "").strip()
                typed_ref = (
                    f"{block_type}:{bare_ref}" if block_type and bare_ref else bare_ref
                )
            name = str(block.get("name") or typed_ref or "candidate").strip()
            one_liner = str(block.get("oneLiner") or "").strip()
            use = str(
                block.get("thought")
                or block.get("useHint")
                or block.get("useWhen")
                or ""
            ).strip()
            detail = f" — {one_liner}" if one_liner else ""
            if use:
                detail += f" Use: {use}"
            rows.append(f"  - `{typed_ref}` · {name}{detail}")
            count += 1
        if rows:
            lines.append(f"- {facet}\n" + "\n".join(rows))
        if count >= MAX_CONTEXT_BLOCKS:
            break
    return lines, count


def _format_context(data: Mapping[str, Any], *, agent_ref: str) -> tuple[str, int]:
    lines, count = _block_lines(data)
    gaps = data.get("gaps")
    gap_lines = (
        [str(gap).strip() for gap in gaps if str(gap).strip()]
        if isinstance(gaps, list)
        else []
    )
    parts = [
        "[Katailyst2 mission context — candidate building blocks, not mandatory instructions]",
        f"Runtime identity: `{agent_ref}`",
    ]
    if lines:
        parts.append("Useful candidates:\n" + "\n".join(lines))
    else:
        parts.append(
            "The well returned no useful registry block for this mission; compose directly."
        )
    if gap_lines:
        parts.append("Known gaps:\n" + "\n".join(f"- {gap}" for gap in gap_lines[:4]))
    parts.append(
        "Judge the candidates yourself. Open a ref for its full body when useful; "
        "use, adapt, or ignore it, and do not call the well again this turn."
    )
    context = "\n\n".join(parts)
    if len(context) > MAX_CONTEXT_CHARS:
        context = (
            context[: MAX_CONTEXT_CHARS - 80].rstrip()
            + "\n\n[Context clipped at the bounded hook limit.]"
        )
    return context, count


def _format_search_context(
    data: Mapping[str, Any], *, agent_ref: str
) -> tuple[str, int]:
    candidates = data.get("candidates")
    return _format_context(
        {
            "dives": [
                {"facet": "Compact registry fallback", "blocks": candidates or []}
            ]
        },
        agent_ref=agent_ref,
    )


def _pending_context(
    agent_ref: str, run_id: str, poll_tool: str, poll_after_seconds: Any
) -> str:
    timing = f" after about {poll_after_seconds}s" if poll_after_seconds else " later"
    return (
        "[Katailyst2 mission context — durable draw started without delaying this turn]\n\n"
        f"Runtime identity: `{agent_ref}`\n\n"
        f"The model-judged Well draw is already running as `{run_id}`. Do not "
        "start another draw or wait before working. Use the active runtime pack, "
        "direct K2 reads, and your own reasoning now. If the deeper roster would "
        f"materially improve the result, poll `{poll_tool}` once{timing} with "
        f"`{{\"runId\":\"{run_id}\"}}`; otherwise finish without ceremony."
    )


def mission_idempotency_key(
    *, agent_ref: str, mission: str, session_id: str = "", turn_id: str = ""
) -> str:
    """Bind retries of one Hermes turn to one durable Well run."""
    material = "\0".join((agent_ref, session_id, turn_id, mission))
    return "hermes:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def draw_mission_context(
    url: str,
    token: str,
    *,
    mission: str,
    agent_ref: str,
    idempotency_key: str = "",
    timeout: float = MISSION_CONTEXT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Start one durable Well draw and return prompt context within one deadline."""
    started = time.monotonic()
    deadline = started + max(0.25, timeout)
    result: dict[str, Any] = {
        "status": "not_configured",
        "mode": "none",
        "context": "",
        "block_count": 0,
        "well_calls": 0,
        "latency_ms": None,
        "error": "",
    }
    if not url or not token:
        result["context"] = (
            "[Katailyst2 mission context unavailable: the hosted K2 endpoint or "
            "agent-bound token is not configured. Continue from the booted runtime "
            "pack and your own reasoning; do not claim a mission draw occurred.]"
        )
        return result

    request_id = 0

    def rpc(method: str, params: dict[str, Any], session_id: str = ""):
        nonlocal request_id
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Katailyst mission-context deadline expired")
        request_id += 1
        return _post(
            url,
            token,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            session_id=session_id,
            timeout=max(0.05, remaining),
        )

    def call_tool(name: str, arguments: dict[str, Any], session_id: str):
        called, _, _ = rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
            session_id,
        )
        tool_result = called.get("result")
        tool_result = tool_result if isinstance(tool_result, Mapping) else {}
        if called.get("error") or tool_result.get("isError") is True:
            raise RuntimeError(f"{name}: {str(called.get('error') or 'isError')[:200]}")
        return _tool_data(tool_result)

    def registry_fallback(
        surface: Mapping[str, str], session_id: str, *, error: str = ""
    ) -> dict[str, Any]:
        search = call_tool(
            surface["registry_search"],
            {"query": mission[:600], "limit": 8, "format": "compact"},
            session_id,
        )
        context, block_count = _format_search_context(search, agent_ref=agent_ref)
        result.update(
            {
                "status": "loaded",
                "mode": "registry_search_fallback",
                "context": context,
                "block_count": block_count,
                "error": error,
            }
        )
        return result

    try:
        initialized, session_id, headers = rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "hlt-hermes-k2-context", "version": "1.0.0"},
            },
        )
        if initialized.get("error"):
            raise RuntimeError(str(initialized["error"]))
        if headers.get("x-katailyst-repo", "").strip().lower() != "katailyst2":
            raise RuntimeError("configured endpoint is not Katailyst2")
        surface = _tool_surface(url, token, session_id=session_id, rpc=rpc)
        well_arguments = {
            "mission": mission,
            "budget": 8,
            "thoughts": True,
            "traverse": False,
        }

        if surface["well_start"] and surface["well_get"]:
            result["mode"] = "async"
            result["well_calls"] = 1
            start_arguments = dict(well_arguments)
            if idempotency_key:
                start_arguments["idempotencyKey"] = idempotency_key[:200]
            try:
                started_run = call_tool(
                    surface["well_start"], start_arguments, session_id
                )
                run_id = str(started_run.get("runId") or "").strip()
                if not run_id:
                    raise RuntimeError("katailyst.well.start returned no runId")
                poll_after = started_run.get("pollAfterSeconds")
                run_status = str(started_run.get("status") or "queued").lower()
                if run_status in {"failed", "cancelled"}:
                    raise RuntimeError(f"katailyst.well async run {run_status}")
                terminal = (
                    started_run.get("result") if run_status == "succeeded" else None
                )
                if run_status == "succeeded" and not isinstance(terminal, Mapping):
                    raise RuntimeError("katailyst.well succeeded without a result")
                if terminal is not None:
                    context, block_count = _format_context(
                        terminal, agent_ref=agent_ref
                    )
                    result.update(
                        status="loaded", context=context, block_count=block_count
                    )
                    return result
                result.update(
                    status="pending",
                    mode="async_pending",
                    context=_pending_context(
                        agent_ref,
                        run_id,
                        (
                            "mcp__katailyst2__"
                            + surface["well_get"].replace(".", "_")
                        ),
                        poll_after,
                    ),
                )
                return result
            except Exception as async_exc:
                if surface["registry_search"] and time.monotonic() < deadline:
                    try:
                        return registry_fallback(
                            surface,
                            session_id,
                            error=(
                                f"{type(async_exc).__name__}: "
                                f"{str(async_exc)[:180]}"
                            ),
                        )
                    finally:
                        # The fallback kept this turn useful, but the failed
                        # durable start may mean this cached discovery surface
                        # is stale. Re-list next turn instead of repeatedly
                        # routing through a broken async tool.
                        _evict_tool_surface(url, token)
                raise

        if surface["registry_search"]:
            return registry_fallback(surface, session_id)

        # Compatibility-only server. The same hard deadline still prevents a
        # legacy synchronous draw from consuming the user's whole turn.
        result["mode"] = "sync_compat"
        result["well_calls"] = 1
        data = call_tool(surface["well_sync"], well_arguments, session_id)
        context, block_count = _format_context(data, agent_ref=agent_ref)
        result.update(
            {
                "status": "loaded",
                "context": context,
                "block_count": block_count,
                "error": "",
            }
        )
        return result
    except Exception as exc:  # the well fuels a turn; it never sinks one
        _evict_tool_surface(url, token)
        result["status"] = "unavailable"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        result["context"] = (
            "[Katailyst2 mission context was unavailable for this turn. Continue "
            "from the active runtime pack, available tools, and your own reasoning; "
            "do not claim K2 returned task-specific blocks.]"
        )
        return result
    finally:
        result["latency_ms"] = round((time.monotonic() - started) * 1000)


def _reset_tool_cache_for_tests() -> None:
    with _CACHE_LOCK:
        _TOOL_CACHE.clear()
