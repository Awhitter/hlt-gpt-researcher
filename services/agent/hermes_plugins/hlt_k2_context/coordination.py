"""Katailyst2-owned Slack coordination; local admission never elects a lead."""
from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .runtime_context import MCP_PROTOCOL_VERSION, _post, _tool_data

COORDINATION_TIMEOUT_SECONDS = 11.0


def claim_coordination(url: str, token: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if not url or not token:
        raise RuntimeError("Katailyst2 coordination is not configured")
    deadline = time.monotonic() + COORDINATION_TIMEOUT_SECONDS
    request_id = 0

    def rpc(method, params, session_id=""):
        nonlocal request_id
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Katailyst2 coordination deadline expired")
        request_id += 1
        result, session_id, headers = _post(
            url, token, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            session_id=session_id, timeout=remaining,
        )
        if result.get("error"):
            raise RuntimeError("Katailyst2 coordination RPC failed")
        return result, session_id, headers

    _, session_id, headers = rpc("initialize", {
        "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "hlt-hermes-coordination", "version": "1.0.0"},
    })
    if headers.get("x-katailyst-repo", "").strip().lower() != "katailyst2":
        raise RuntimeError("configured endpoint is not Katailyst2")
    listed, _, _ = rpc("tools/list", {}, session_id)
    names = {
        row.get("name") for row in (listed.get("result") or {}).get("tools", [])
        if isinstance(row, Mapping)
    }
    direct = next((n for n in ("agents.coordination.claim", "agents_coordination_claim") if n in names), None)
    bridge = next((n for n in ("tool.execute", "tool_execute") if n in names), None)
    if direct:
        params = {"name": direct, "arguments": arguments}
    elif bridge:
        params = {"name": bridge, "arguments": {"verb": "agents.coordination.claim", "args": arguments}}
    else:
        raise RuntimeError("Katailyst2 coordination is outside this token's tool surface")
    for attempt in range(3):
        called, _, _ = rpc("tools/call", params, session_id)
        result = called.get("result") or {}
        claim = _tool_data(result)
        if result.get("isError") is not True:
            break
        meta = claim.get("meta") or {}
        if meta.get("reason") != "coordination_busy" or attempt == 2:
            raise RuntimeError("Katailyst2 coordination claim failed")
        delay = min(max(float(meta.get("retryAfterMs") or 1000) / 1000, 0.1), 1.0)
        if time.monotonic() + delay >= deadline:
            raise TimeoutError("Katailyst2 coordination deadline expired")
        time.sleep(delay)
    # Generic tool.execute wraps the canonical decision under output.
    if isinstance(claim.get("output"), Mapping):
        claim = dict(claim["output"])
    source = claim.get("source") or {}
    expected = arguments["source"]
    if (
        claim.get("schemaVersion") != "agent_coordination.v1"
        or not claim.get("coordinationId")
        or claim.get("callerRole") not in {"lead", "contributor", "observer"}
        or not isinstance(claim.get("mayRespond"), bool)
        or not isinstance(claim.get("mayComplete"), bool)
        or any(source.get(k) != expected.get(k) for k in ("platform", "teamId", "channelId", "threadTs", "messageTs"))
    ):
        raise RuntimeError("Katailyst2 coordination returned an invalid decision")
    return dict(claim)


def coordination_context(claim: Mapping[str, Any]) -> str:
    return (
        "[Katailyst2 coordination]\n"
        f"coordinationId: {claim['coordinationId']}; revision: {claim.get('revision')}; "
        f"your role: {claim['callerRole']}; lead: {claim.get('leadAgentRef')}; "
        f"mayComplete: {str(claim['mayComplete']).lower()}.\n"
        "Keep the original human task. Collaborate through durable agent handoffs, never recruit "
        "or react through bot-authored Slack messages. Contributors provide their assigned evidence "
        "and artifacts to the lead; the lead owns synthesis and completion. Before completion or "
        "handoff, refresh agents.coordination.get using this coordinationId and your callerAgentRef; "
        "obey its current role and revision. Include coordinationId in dependent agent runs."
    )
