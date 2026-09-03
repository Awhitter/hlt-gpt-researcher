"""Katailyst2 mission-context hook for the pinned Hermes runtime."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .runtime_context import (
    draw_mission_context,
    is_substantive_mission,
    mission_idempotency_key,
)
from .slack_agent_lead import (
    ROSTER_NONPARTICIPANT_REFS,
    load_fallback_roster,
    select_slack_agent_lead,
)
from .slack_lead_ledger import RECEIPT_SCHEMA, SlackLeadLedger

logger = logging.getLogger(__name__)

HOSTED_K2_CONTEXT = (
    "[Katailyst2 hosted mission — bounded handoff already supplied] "
    "K2 has already provided the mission context and any selected context refs in "
    "this turn. Do not call katailyst.well again. Follow the per-run retrieval and "
    "final-answer budget in the system instructions; use supplied refs directly, "
    "allow at most one focused recovery search, and return a useful final before "
    "the deadline."
)


def _agent_ref() -> str:
    configured = os.getenv("HLT_AGENT_REF", "").strip()
    if configured:
        return configured
    agent_id = os.getenv("AGENT_ID", "cleo").strip().lower() or "cleo"
    return f"agent:{agent_id}"


def _slack_key(event: Any, raw_message: Mapping[str, Any]) -> tuple[str, str, str]:
    metadata = getattr(event, "metadata", None)
    metadata = metadata if isinstance(metadata, Mapping) else {}
    source = getattr(event, "source", None)
    workspace_id = str(
        raw_message.get("team")
        or raw_message.get("team_id")
        or metadata.get("slack_team_id")
        or getattr(source, "scope_id", "")
        or ""
    ).strip()
    channel_id = str(
        raw_message.get("channel")
        or metadata.get("slack_channel_id")
        or getattr(source, "chat_id", "")
        or ""
    ).strip()
    message_ts = str(
        raw_message.get("ts") or getattr(event, "message_id", "") or ""
    ).strip()
    return workspace_id, channel_id, message_ts


def _lead_ledger() -> SlackLeadLedger:
    home = Path(os.getenv("HERMES_HOME", "/data/hermes"))
    return SlackLeadLedger(home / "slack-agent-lead.sqlite3")


def _private_receipt(
    *,
    decision: Any,
    workspace_id: str,
    channel_id: str,
    message_ts: str,
) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "workspaceId": workspace_id,
        "channelId": channel_id,
        "messageTs": message_ts,
        "channelKind": decision.channel_kind,
        "localAgentRef": decision.local_agent_ref,
        "selectedAgentRef": decision.selected_agent_ref,
        "recognizedMentions": list(decision.recognized_mentions),
        "action": decision.action,
        "reason": decision.reason,
        "rosterSha256": decision.roster_sha256,
    }


def _pre_gateway_dispatch(event: Any = None, **_: Any) -> dict[str, str] | None:
    """Admit only Cleo-owned Slack turns before model and typing dispatch."""
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    platform_value = getattr(platform, "value", platform)
    if str(platform_value or "").lower() != "slack":
        return None

    # Native slash invocations and message-form control commands already
    # address one installed app and must retain their local session semantics
    # (/stop, /approve, /reset, /hermes). They are not ambient Slack turns.
    raw = getattr(event, "raw_message", None)
    raw_message = raw if isinstance(raw, Mapping) else {}
    message_type = getattr(getattr(event, "message_type", None), "value", None)
    edited_message = bool(raw_message.get("_slack_changed_event_ts")) or (
        str(raw_message.get("subtype") or "") == "message_changed"
    )
    if raw_message.get("command") or (
        str(message_type or "").lower() == "command" and not edited_message
    ):
        return None

    local_agent_ref = _agent_ref()
    if local_agent_ref in ROSTER_NONPARTICIPANT_REFS:
        return None

    try:
        roster = load_fallback_roster()
        decision = select_slack_agent_lead(
            raw_message,
            local_agent_ref=local_agent_ref,
            roster=roster,
        )
        workspace_id, channel_id, message_ts = _slack_key(event, raw_message)
        receipt = _private_receipt(
            decision=decision,
            workspace_id=workspace_id,
            channel_id=channel_id,
            message_ts=message_ts,
        )
    except Exception as exc:  # noqa: BLE001 - hook faults must fail closed
        failure = {
            "schema": RECEIPT_SCHEMA,
            "localAgentRef": local_agent_ref,
            "action": "suppress",
            "reason": "lead_selection_unavailable",
            "errorType": type(exc).__name__,
        }
        logger.error("%s %s", RECEIPT_SCHEMA, json.dumps(failure, sort_keys=True))
        return {"action": "skip", "reason": "lead_selection_unavailable"}

    try:
        tombstone = _lead_ledger().record_once(
            workspace_id=workspace_id,
            channel_id=channel_id,
            message_ts=message_ts,
            receipt=receipt,
        )
    except Exception as exc:  # noqa: BLE001 - any ledger fault must fail closed
        failure = {
            **receipt,
            "action": "suppress",
            "reason": "lead_ledger_unavailable",
            "errorType": type(exc).__name__,
        }
        logger.error("%s %s", RECEIPT_SCHEMA, json.dumps(failure, sort_keys=True))
        return {"action": "skip", "reason": "lead_ledger_unavailable"}

    if not tombstone.inserted:
        replay = {
            **tombstone.receipt,
            "action": "suppress",
            "reason": "durable_replay_tombstone",
            "replay": True,
        }
        logger.info("%s %s", RECEIPT_SCHEMA, json.dumps(replay, sort_keys=True))
        return {"action": "skip", "reason": "durable_replay_tombstone"}

    logger.info("%s %s", RECEIPT_SCHEMA, json.dumps(receipt, sort_keys=True))
    if decision.allows_dispatch:
        # None means normal dispatch without short-circuiting a later policy
        # hook; Hermes stops evaluating hooks after an explicit allow result.
        return None
    return {"action": "skip", "reason": decision.reason}


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

    # K2's durable-run bridge already carries the canonical mission reading,
    # explicit refs, and execution budget. A second automatic wishing-well draw
    # is duplicate discovery. More importantly, a slow draw used 16 seconds of
    # a real 20-second mission before the model saw the task. Keep this branch
    # network-free; the run-specific system prompt owns the exact time budget.
    if (
        str(platform or "").strip().lower() == "api_server"
        and str(session_id or "").startswith("hook:k2:")
    ):
        logger.info(
            "K2 mission context status=handoff_supplied blocks=0 latency_ms=0 "
            "platform=%s session=%s turn=%s",
            platform,
            session_id[:24],
            turn_id[:24],
        )
        return {"context": HOSTED_K2_CONTEXT}

    result = draw_mission_context(
        os.getenv("KATAILYST2_MCP_URL", "").strip(),
        os.getenv("KATAILYST2_MCP_TOKEN", "").strip(),
        mission=mission,
        agent_ref=_agent_ref(),
        idempotency_key=mission_idempotency_key(
            agent_ref=_agent_ref(),
            mission=mission,
            session_id=str(session_id or ""),
            turn_id=str(turn_id or ""),
        ),
    )
    logger.info(
        "K2 mission context status=%s mode=%s blocks=%s "
        "latency_ms=%s platform=%s session=%s turn=%s",
        result.get("status"),
        result.get("mode"),
        result.get("block_count"),
        result.get("latency_ms"),
        platform or "unknown",
        session_id[:24],
        turn_id[:24],
    )
    context = result.get("context")
    return {"context": context} if isinstance(context, str) and context else None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
