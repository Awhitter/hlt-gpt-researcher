"""Katailyst2 mission-context hook for the pinned Hermes runtime."""
from __future__ import annotations

import logging
import os
from typing import Any

from .runtime_context import draw_mission_context, is_substantive_mission

logger = logging.getLogger(__name__)


def _agent_ref() -> str:
    configured = os.getenv("HLT_AGENT_REF", "").strip()
    if configured:
        return configured
    agent_id = os.getenv("AGENT_ID", "cleo").strip().lower() or "cleo"
    return f"agent:{agent_id}"


def _pre_llm_call(
    user_message: str = "",
    platform: str = "",
    session_id: str = "",
    turn_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    """Return ephemeral K2 context once per real user turn.

    Hermes invokes ``pre_llm_call`` once while assembling a turn, before its
    model/tool loop. The returned context is added to the current user message
    and is never persisted into transcript history, so K2 discovery improves
    this mission without slowly clogging the agent's durable memory.
    """
    mission = str(user_message or "").strip()
    if not is_substantive_mission(mission):
        return None

    result = draw_mission_context(
        os.getenv("KATAILYST2_MCP_URL", "").strip(),
        os.getenv("KATAILYST2_MCP_TOKEN", "").strip(),
        mission=mission,
        agent_ref=_agent_ref(),
    )
    logger.info(
        "K2 mission context status=%s blocks=%s latency_ms=%s platform=%s "
        "session=%s turn=%s",
        result.get("status"),
        result.get("block_count"),
        result.get("latency_ms"),
        platform or "unknown",
        session_id[:24],
        turn_id[:24],
    )
    context = result.get("context")
    return {"context": context} if isinstance(context, str) and context else None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", _pre_llm_call)
