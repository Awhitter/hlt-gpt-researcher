"""One bounded Katailyst2 wishing-well draw for a Hermes mission.

This module is deliberately self-contained inside the user plugin directory:
Hermes loads user plugins from its durable home and must not depend on the
wrapper process's import path. It uses streamable HTTP directly, exactly like
the boot readiness probe, and invokes ``katailyst.well`` at most once per call.
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
WELL_NAMES = ("katailyst.well", "katailyst_well")

_CACHE_LOCK = threading.Lock()
_TOOL_CACHE: dict[tuple[str, str], str] = {}
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


def _tool_name(
    url: str,
    token: str,
    *,
    session_id: str,
    rpc: Any,
) -> str:
    key = _cache_key(url, token)
    with _CACHE_LOCK:
        cached = _TOOL_CACHE.get(key, "")
    if cached:
        return cached
    listed, _, _ = rpc("tools/list", {}, session_id)
    if listed.get("error"):
        raise RuntimeError(str(listed["error"]))
    names = [
        str(tool.get("name") or "")
        for tool in ((listed.get("result") or {}).get("tools") or [])
        if isinstance(tool, Mapping)
    ]
    found = next((name for name in names if name in WELL_NAMES), "")
    if not found:
        raise RuntimeError("katailyst.well is not in this token's tool surface")
    with _CACHE_LOCK:
        _TOOL_CACHE[key] = found
    return found


def _block_lines(data: Mapping[str, Any]) -> tuple[list[str], int]:
    lines: list[str] = []
    count = 0
    dives = data.get("dives")
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


def draw_mission_context(
    url: str,
    token: str,
    *,
    mission: str,
    agent_ref: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Call the current well exactly once and return bounded prompt context."""
    started = time.monotonic()
    result: dict[str, Any] = {
        "status": "not_configured",
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
        request_id += 1
        return _post(
            url,
            token,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            session_id=session_id,
            timeout=timeout,
        )

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
        tool_name = _tool_name(url, token, session_id=session_id, rpc=rpc)
        result["well_calls"] = 1
        called, _, _ = rpc(
            "tools/call",
            {
                "name": tool_name,
                "arguments": {
                    "mission": mission,
                    "budget": 8,
                    "thoughts": True,
                    "traverse": False,
                },
            },
            session_id,
        )
        tool_result = called.get("result") or {}
        if called.get("error") or tool_result.get("isError"):
            with _CACHE_LOCK:
                _TOOL_CACHE.pop(_cache_key(url, token), None)
            raise RuntimeError(
                str(called.get("error") or "katailyst.well returned isError")
            )
        data = _tool_data(tool_result)
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
