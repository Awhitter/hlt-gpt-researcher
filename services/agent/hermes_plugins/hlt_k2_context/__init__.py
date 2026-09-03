"""Katailyst2 mission-context hook for the pinned Hermes runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
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

SPILLOVER_DEFAULT_PAGE_CHARS = 8_000
SPILLOVER_MAX_PAGE_CHARS = 12_000
_SAFE_SPILLOVER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,220}\.txt")
SLACK_TOOL_ROUND_LIMIT = 5
_TOOL_BUDGET_TTL_SECONDS = 60 * 60
_TOOL_BUDGET_MAX_TURNS = 256
_TOOL_BUDGET_LOCK = threading.Lock()
_TOOL_BUDGETS: dict[str, dict[str, Any]] = {}
_TOOL_BUDGET_BLOCK_MESSAGE = (
    "Slack foreground tool budget reached after five tool-calling rounds. "
    "Do not call another tool in this turn. Return one useful final answer now "
    "from the evidence already collected, and label any missing value unknown."
)

HOSTED_K2_CONTEXT = (
    "[Katailyst2 hosted mission — bounded handoff already supplied] "
    "K2 has already provided the mission context and any selected context refs in "
    "this turn. Do not call katailyst.well again. Follow the per-run retrieval and "
    "final-answer budget in the system instructions; use supplied refs directly, "
    "allow at most one focused recovery search, and return a useful final before "
    "the deadline."
)


def _spillover_session_prefix(session_id: str) -> str:
    """Mirror the pinned Hermes prefix without exposing the session id."""
    raw_session_id = str(session_id or "")
    if not raw_session_id:
        return ""
    return hashlib.sha256(raw_session_id.encode("utf-8")).hexdigest()[:20]


def _tool_budget_key(*, turn_id: str = "", session_id: str = "") -> str:
    return str(turn_id or session_id or "").strip()


def _start_slack_tool_budget(*, turn_id: str = "", session_id: str = "") -> None:
    """Start one bounded tool-round ledger for an interactive Slack turn."""
    key = _tool_budget_key(turn_id=turn_id, session_id=session_id)
    if not key:
        return
    now = time.monotonic()
    with _TOOL_BUDGET_LOCK:
        stale = [
            item_key
            for item_key, state in _TOOL_BUDGETS.items()
            if now - float(state.get("started_at", now)) > _TOOL_BUDGET_TTL_SECONDS
        ]
        for item_key in stale:
            _TOOL_BUDGETS.pop(item_key, None)
        if len(_TOOL_BUDGETS) >= _TOOL_BUDGET_MAX_TURNS:
            oldest = min(
                _TOOL_BUDGETS,
                key=lambda item_key: float(
                    _TOOL_BUDGETS[item_key].get("started_at", now)
                ),
            )
            _TOOL_BUDGETS.pop(oldest, None)
        _TOOL_BUDGETS[key] = {
            "started_at": now,
            "rounds": set(),
            "blocked_rounds": set(),
        }


def _pre_tool_call(
    tool_name: str = "",
    session_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    """Bound Slack tool rounds while preserving parallel calls in each round."""
    key = _tool_budget_key(turn_id=turn_id, session_id=session_id)
    round_id = str(api_request_id or tool_call_id or "").strip()
    if not key or not round_id:
        return None
    with _TOOL_BUDGET_LOCK:
        state = _TOOL_BUDGETS.get(key)
        if state is None:
            return None
        rounds = state["rounds"]
        if round_id in rounds:
            return None
        if len(rounds) >= SLACK_TOOL_ROUND_LIMIT:
            blocked_rounds = state["blocked_rounds"]
            if round_id not in blocked_rounds:
                blocked_rounds.add(round_id)
                logger.info(
                    "Slack tool-round budget reached: turn=%s rounds=%s tool=%s",
                    key[:32],
                    len(rounds),
                    tool_name,
                )
            return {"action": "block", "message": _TOOL_BUDGET_BLOCK_MESSAGE}
        rounds.add(round_id)
    return None


def _read_spillover(args: Any = None, **context: Any) -> str:
    """Read one bounded page from a Hermes-owned persisted tool result.

    Slack intentionally has no general file toolset because that also grants
    writes. Oversized MCP results are nevertheless stored under
    ``$HERMES_HOME/cache/spillover``. This narrow reader accepts only a saved
    result's basename (or the exact path shown in ``persisted-output``), cannot
    traverse elsewhere, requires the originating session, and never mutates
    the file.
    """
    values = args if isinstance(args, Mapping) else {}
    raw_handle = str(values.get("handle") or "").strip()
    filename = Path(raw_handle).name
    if not raw_handle or not _SAFE_SPILLOVER_NAME.fullmatch(filename):
        return json.dumps(
            {"error": "handle must be a .txt result path from persisted-output"}
        )

    session_prefix = _spillover_session_prefix(
        str(context.get("session_id") or "")
    )
    if not session_prefix or not filename.startswith(f"{session_prefix}_"):
        return json.dumps({"error": "saved result does not belong to this session"})

    try:
        offset = int(values.get("offset", 0))
        limit = int(values.get("limit", SPILLOVER_DEFAULT_PAGE_CHARS))
    except (TypeError, ValueError):
        return json.dumps({"error": "offset and limit must be integers"})
    if offset < 0:
        return json.dumps({"error": "offset must be zero or greater"})
    limit = max(1, min(limit, SPILLOVER_MAX_PAGE_CHARS))

    root = (Path(os.getenv("HERMES_HOME", "/data/hermes")) / "cache" / "spillover")
    try:
        root = root.resolve(strict=True)
        path = (root / filename).resolve(strict=True)
    except OSError:
        return json.dumps({"error": "saved result is unavailable or expired"})
    if path.parent != root or not path.is_file():
        return json.dumps(
            {"error": "saved result path is outside the spillover store"}
        )

    try:
        total_bytes = path.stat().st_size
        if offset > total_bytes:
            return json.dumps({"error": "offset exceeds the saved result size"})
        with path.open("rb") as handle:
            handle.seek(offset)
            raw_page = handle.read(limit + 4)
    except OSError:
        return json.dumps({"error": "saved result could not be read"})

    # Offsets are bytes so a late page never rereads the whole file. The
    # persisted result is valid UTF-8; extend by at most three bytes to finish
    # the final code point. Reject caller-chosen offsets inside a code point.
    target = min(limit, len(raw_page))
    page = None
    consumed = 0
    for end in range(target, min(len(raw_page), target + 3) + 1):
        try:
            page = raw_page[:end].decode("utf-8")
        except UnicodeDecodeError:
            continue
        consumed = end
        break
    if page is None:
        return json.dumps({"error": "offset is not aligned to UTF-8 content"})

    next_offset = offset + consumed
    return json.dumps(
        {
            "schema": "hlt_spillover_page.v1",
            "handle": filename,
            "offset": offset,
            "returnedBytes": consumed,
            "totalBytes": total_bytes,
            "hasMore": next_offset < total_bytes,
            "nextOffset": next_offset if next_offset < total_bytes else None,
            "content": page,
        },
        ensure_ascii=False,
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
    if str(platform or "").strip().lower() == "slack":
        _start_slack_tool_budget(turn_id=turn_id, session_id=session_id)
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
    description = (
        "Read one bounded page from a large tool result Hermes already saved. "
        "Pass the exact path or filename shown inside persisted-output plus an "
        "optional byte offset and limit. Read-only and spillover-scoped."
    )
    ctx.register_tool(
        name="read_spillover",
        toolset="hlt-context",
        schema={
            "name": "read_spillover",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "Exact saved .txt path or filename from persisted-output.",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "UTF-8 byte offset returned by the prior page.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": SPILLOVER_MAX_PAGE_CHARS,
                        "default": SPILLOVER_DEFAULT_PAGE_CHARS,
                        "description": (
                            "Maximum UTF-8 bytes to return, plus a complete "
                            "final code point."
                        ),
                    },
                },
                "required": ["handle"],
                "additionalProperties": False,
            },
        },
        handler=_read_spillover,
        description=description,
        emoji="📄",
    )
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
