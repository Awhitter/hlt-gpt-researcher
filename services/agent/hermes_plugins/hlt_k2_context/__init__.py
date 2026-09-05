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
    is_human_authored_message,
    load_fallback_roster,
    select_slack_agent_lead,
)
from .slack_lead_ledger import RECEIPT_SCHEMA, SlackLeadLedger

logger = logging.getLogger(__name__)

SPILLOVER_DEFAULT_PAGE_CHARS = 8_000
SPILLOVER_MAX_PAGE_CHARS = 12_000
SPILLOVER_MAX_BODY_SOURCE_BYTES = 1_048_576
_SAFE_SPILLOVER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,220}\.txt")
SLACK_TOOL_ROUND_LIMIT = 5
EFFECT_POLICY_VERSION = "agent_effect_policy.v1"
_TOOL_BUDGET_TTL_SECONDS = 60 * 60
_TOOL_BUDGET_MAX_TURNS = 256
_TOOL_BUDGET_LOCK = threading.Lock()
_TOOL_BUDGETS: dict[str, dict[str, Any]] = {}
_TOOL_BUDGET_BLOCK_MESSAGE = (
    "Slack foreground tool budget reached after five tool-calling rounds. "
    "Do not call another tool in this turn. Return one useful final answer now "
    "from the evidence already collected, and label any missing value unknown."
)

_EFFECT_APPROVAL_WORDS = frozenset(
    {
        "send",
        "sent",
        "postmessage",
        "sendmessage",
        "deliver",
        "upload",
        "share",
        "publish",
        "published",
        "delete",
        "destroy",
        "purchase",
        "buy",
        "charge",
        "spend",
        "rotate",
        "revoke",
        "grant",
        "invite",
    }
)
_EFFECT_ARGUMENT_KEYS = frozenset(
    {"action", "operation", "method", "verb", "tool", "toolref", "target", "label"}
)
_EFFECT_OPERATION_KEYS = frozenset({"action", "operation", "method", "verb"})
_COMPUTER_READ_ONLY_ACTIONS = frozenset(
    {"capture", "wait", "listapps", "listwindows"}
)
_COMPUTER_MUTATING_ACTIONS = frozenset(
    {
        "click",
        "doubleclick",
        "rightclick",
        "middleclick",
        "drag",
        "scroll",
        "type",
        "key",
        "setvalue",
        "focusapp",
    }
)
_SHELL_EFFECT_RE = re.compile(
    r"(?:\brm\b|\bgit\b[^\n;&|]*\bpush\b|\bgh\b[^\n;&|]*\bpr\b[^\n;&|]*\bmerge\b|"
    r"\bgh\b[^\n;&|]*\bapi\b[^\n;&|]*(?:--method|-X)\s*(?:POST|PUT|PATCH|DELETE)\b|"
    r"\bcurl\b[^\n]*(?:--request|-X)\s*(?:POST|PUT|PATCH|DELETE)\b|"
    r"\bdoppler\b[^\n]*(?:set|delete|rotate)\b)",
    re.IGNORECASE,
)
_NETWORK_SEND_RE = re.compile(
    r"(?:\bcurl\b[^\n;&|]*(?:"
    r"\s(?:-d|-F|-T)(?:\s|=|[^\s]*)|"
    r"\s--(?:data(?:-[a-z-]+)?|form(?:-[a-z-]+)?|json|upload-file)(?:\s|=))|"
    r"\bwget\b[^\n;&|]*\s--(?:post-data|post-file|body-data|body-file)(?:\s|=)|"
    r"\bwget\b[^\n;&|]*\s--method(?:\s|=)(?:POST|PUT|PATCH|DELETE)\b)",
)
_GH_PUBLISH_RE = re.compile(
    r"\bgh\b[^\n;&|]*\b(?:issue|pr|release)\b[^\n;&|]*\bcreate\b",
    re.IGNORECASE,
)
_GH_SEND_RE = re.compile(
    r"\bgh\b[^\n;&|]*\b(?:issue|pr)\b[^\n;&|]*\b(?:comment|review)\b",
    re.IGNORECASE,
)
_PRODUCTION_COMMAND_RE = re.compile(
    r"(?:\bvercel\b[^\n;&|]*\bdeploy\b|\brender\b[^\n;&|]*\bdeploy\b|"
    r"\b(?:npm|pnpm)\b[^\n;&|]*\bpublish\b|"
    r"\bkubectl\b[^\n;&|]*\b(?:apply|delete|rollout)\b|"
    r"\b(?:fly|railway|heroku)\b[^\n]*(?:deploy|up|release|promote)\b)",
    re.IGNORECASE,
)

_AGENTMAIL_READ_ACTIONS = frozenset(
    {
        "status",
        "authme",
        "listinboxes",
        "listmessages",
        "searchmessages",
        "getmessage",
        "listthreads",
        "searchthreads",
        "getthread",
        "getattachment",
        "listdrafts",
        "getdraft",
    }
)
_AGENTMAIL_DRAFT_ACTIONS = frozenset({"createdraft", "updatedraft"})
_AGENTMAIL_REVERSIBLE_ACTIONS = frozenset(
    {"updatemessagelabels", "updatethreadlabels", "unscheduledraft"}
)
_AGENTMAIL_SEND_ACTIONS = frozenset(
    {"scheduledraft", "senddraft", "send", "reply", "forward"}
)


def _effect_words(value: Any) -> set[str]:
    return {
        word
        for word in re.split(r"[^a-z0-9]+", str(value or "").lower())
        if word
    }


def effect_policy_decision(tool_name: str, args: Any = None) -> dict[str, str] | None:
    """Require approval for the effect, not for access to a capable tool.

    Reads, research, drafts, staging, file/code work, and reversible internal
    configuration remain automatic. The decision is intentionally based on
    the concrete tool plus its requested operation so K2 discovery or an
    AgentMail draft cannot be mistaken for an external send.
    """
    normalized_tool = str(tool_name or "").strip().lower()
    values = args if isinstance(args, Mapping) else {}
    tool_words = _effect_words(normalized_tool)

    action_words: set[str] = set()
    operation_words: set[str] = set()
    operation_names: set[str] = set()
    for key, value in values.items():
        normalized_key = str(key).lower()
        if normalized_key in _EFFECT_ARGUMENT_KEYS and isinstance(
            value, (str, int, float, bool)
        ):
            action_words.update(_effect_words(value))
            if normalized_key in _EFFECT_OPERATION_KEYS:
                operation_words.update(_effect_words(value))
                operation_names.add(re.sub(r"[^a-z0-9]", "", str(value).lower()))
    # Progressive K2 execution carries the real provider tool and operation in
    # nested arguments. Inspect only action/identity fields, never free-form
    # prompt prose ("draft an email to send later" is still just a draft).
    nested = values.get("args")
    if isinstance(nested, Mapping):
        for key, value in nested.items():
            normalized_key = str(key).lower()
            if normalized_key in _EFFECT_ARGUMENT_KEYS and isinstance(
                value, (str, int, float, bool)
            ):
                action_words.update(_effect_words(value))
                if normalized_key in _EFFECT_OPERATION_KEYS:
                    operation_words.update(_effect_words(value))
                    operation_names.add(
                        re.sub(r"[^a-z0-9]", "", str(value).lower())
                    )

    category = ""
    risky_words = (tool_words | action_words) & _EFFECT_APPROVAL_WORDS
    if risky_words:
        if risky_words & {
            "send",
            "sent",
            "postmessage",
            "sendmessage",
            "deliver",
            "upload",
            "share",
        }:
            category = "external_send"
        elif risky_words & {"publish", "published"}:
            category = "external_publish"
        elif risky_words & {"delete", "destroy"}:
            category = "delete"
        elif risky_words & {"purchase", "buy", "charge", "spend"}:
            category = "spend"
        elif risky_words & {"rotate", "revoke"}:
            category = "credential_change"
        elif risky_words & {"grant", "invite"}:
            category = "access_grant"

    if normalized_tool in {
        "image_generate",
        "video_generate",
        "text_to_speech",
        "tts",
    }:
        category = "spend"

    agentmail_seam = "agentmail" in tool_words or "agentmail" in action_words
    if agentmail_seam:
        agentmail_actions = set(operation_names)
        compact_tool = re.sub(r"[^a-z0-9]", "", normalized_tool)
        # Direct MCP tools encode the operation in their registered name;
        # governed K2 execution carries it in args.action/operation.
        direct_action_aliases = {
            "status": "status",
            "authme": "authme",
            "inboxeslist": "listinboxes",
            "messageslist": "listmessages",
            "messagessearch": "searchmessages",
            "messagesget": "getmessage",
            "messagesupdatelabels": "updatemessagelabels",
            "threadslist": "listthreads",
            "threadssearch": "searchthreads",
            "threadsget": "getthread",
            "threadsupdatelabels": "updatethreadlabels",
            "attachmentsget": "getattachment",
            "draftslist": "listdrafts",
            "draftsget": "getdraft",
            "draftscreate": "createdraft",
            "draftsupdate": "updatedraft",
            "draftsdelete": "deletedraft",
            "draftsschedule": "scheduledraft",
            "draftsunschedule": "unscheduledraft",
            "draftssend": "senddraft",
            "messagesreply": "reply",
            "messagesforward": "forward",
            "messagessend": "send",
        }
        for suffix, canonical_action in direct_action_aliases.items():
            if compact_tool.endswith(suffix):
                agentmail_actions.add(canonical_action)
        if agentmail_actions & _AGENTMAIL_SEND_ACTIONS:
            category = "external_send"
        elif "deletedraft" in agentmail_actions:
            category = "delete"
        elif agentmail_actions & (
            _AGENTMAIL_READ_ACTIONS
            | _AGENTMAIL_DRAFT_ACTIONS
            | _AGENTMAIL_REVERSIBLE_ACTIONS
        ):
            # Reads, drafts, and reversible label/schedule state are automatic.
            category = ""
        elif agentmail_actions or agentmail_seam:
            # Unknown AgentMail effects fail closed at this provider boundary;
            # do not guess that a future verb is merely a draft.
            category = "protected_production_change"
    if normalized_tool == "cronjob":
        if action_words & {"remove", "delete"}:
            category = "delete"
        elif action_words & {"create", "resume", "run", "trigger"}:
            category = "spend"
    if normalized_tool == "computer_use":
        if operation_names & _COMPUTER_MUTATING_ACTIONS:
            category = "protected_production_change"
        elif operation_names and not operation_names.issubset(
            _COMPUTER_READ_ONLY_ACTIONS
        ):
            # The computer tool's own contract treats every non-observational
            # action as effectful. Unknown future actions therefore fail closed.
            category = "protected_production_change"
    if normalized_tool in {"browser_click", "browser_console"}:
        category = "protected_production_change"
    if normalized_tool == "browser_press":
        pressed = _effect_words(values.get("key") or values.get("keys") or "")
        observational_keys = {
            "escape",
            "esc",
            "tab",
            "shift",
            "arrowup",
            "arrowdown",
            "arrowleft",
            "arrowright",
            "pageup",
            "pagedown",
            "home",
            "end",
        }
        if pressed and not pressed.issubset(observational_keys):
            category = "protected_production_change"
    if normalized_tool in {"terminal", "execute_code", "code_execution"}:
        command = str(values.get("command") or values.get("code") or "")
        if _GH_PUBLISH_RE.search(command):
            category = "external_publish"
        elif _GH_SEND_RE.search(command) or _NETWORK_SEND_RE.search(command):
            category = "external_send"
        elif _PRODUCTION_COMMAND_RE.search(command) or _SHELL_EFFECT_RE.search(command):
            category = "protected_production_change"

    production_words = {"production", "prod", "live"}
    change_words = {"deploy", "promote", "release"}
    if (tool_words | action_words) & change_words and (
        tool_words & {"vercel", "render", "deploy"}
        or action_words & production_words
    ):
        category = "protected_production_change"

    provider_effect_seam = (
        "tool_execute_effect" in normalized_tool
        or "tool.execute.effect" in normalized_tool
    )
    if provider_effect_seam and not category:
        category = "protected_production_change"

    if not category:
        return None
    labels = {
        "external_send": "send this outside the working draft",
        "external_publish": "publish this externally",
        "delete": "delete data or configuration",
        "spend": "start work that can create new spend",
        "credential_change": "rotate or revoke a credential",
        "access_grant": "grant another person or service access",
        "protected_production_change": (
            "make this protected or user-visible production change"
        ),
    }
    return {
        "action": "approve",
        "message": f"Approval needed before Cleo can {labels[category]}.",
        "rule_key": f"{EFFECT_POLICY_VERSION}:{category}",
    }

HOSTED_K2_CONTEXT = (
    "[Katailyst2 hosted mission — bounded handoff already supplied] "
    "K2 has already provided the mission context and any selected context refs in "
    "this turn. Do not call katailyst.well again. Follow the per-run retrieval and "
    "final-answer budget in the system instructions; use supplied refs directly, "
    "allow at most one focused recovery search, and return a useful final before "
    "the deadline. For a persisted K2 result, load read_spillover through "
    "tool_describe and use tool_call with its saved handle, view:'body', and "
    "returned nextOffset to read the full body. Do not use code or shell to "
    "decode an already-saved result."
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
    args: Any = None,
    session_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    """Apply effect approval, then bound Slack tool rounds."""
    effect_decision = effect_policy_decision(tool_name, args)
    if effect_decision is not None:
        return effect_decision
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


def _spillover_body(value: Any) -> tuple[str, str] | None:
    """Unwrap known K2 result envelopes, not arbitrary expressions or paths."""
    for _ in range(4):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                return None
        if not isinstance(value, Mapping):
            return None
        structured = value.get("structuredContent")
        transport = (
            structured.get("transportCompaction")
            if isinstance(structured, Mapping)
            else value.get("transportCompaction")
        )
        if (
            isinstance(transport, Mapping)
            and transport.get("contractVersion") == "tool_execute_transport.v1"
            and transport.get("mode") == "model_visible_text"
            and transport.get("modelVisibleText") == "content[0].text"
        ):
            # K2's compact structured projection is only metadata. Its fixed
            # content[0].text sibling (or Hermes' saved result text) carries
            # the complete deduplicated envelope. Never evaluate jsonPath.
            content = value.get("content")
            first = content[0] if isinstance(content, list) and content else None
            value = (
                first.get("text")
                if isinstance(first, Mapping) and first.get("type") == "text"
                else value.get("result")
            )
            continue
        for field in ("body_md", "body", "markdown"):
            if isinstance(value.get(field), str) and value[field]:
                return value[field], field
        output = value.get("output")
        if isinstance(output, (Mapping, str)):
            value = output
        else:
            value = structured if isinstance(structured, Mapping) else value.get("result")
    return None


def _read_spillover(args: Any = None, **context: Any) -> str:
    """Read one bounded page from a Hermes-owned persisted tool result.

    Oversized MCP results are stored under ``$HERMES_HOME/cache/spillover``.
    This narrow reader lets the model page its own prior result without using
    a new terminal/file round: it accepts only a saved result's basename (or
    the exact path shown in ``persisted-output``), cannot traverse elsewhere,
    requires the originating session, and never mutates the file. ``view:body``
    decodes a bounded K2 JSON envelope before paging its body, so one-line JSON
    does not force a model to execute code simply to recover its own evidence.
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

    view = values.get("view", "raw")
    if not isinstance(view, str) or view not in {"raw", "body"}:
        return json.dumps({"error": "view must be raw or body"})

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
        source_bytes = total_bytes
        body_field = None
        if view == "body":
            if source_bytes > SPILLOVER_MAX_BODY_SOURCE_BYTES:
                return json.dumps({"error": "saved result exceeds body decode limit; use view:raw byte pages"})
            with path.open("rb") as handle:
                encoded = handle.read(SPILLOVER_MAX_BODY_SOURCE_BYTES + 1)
            if len(encoded) > SPILLOVER_MAX_BODY_SOURCE_BYTES:
                return json.dumps({"error": "saved result exceeds body decode limit; use view:raw byte pages"})
            try:
                decoded = _spillover_body(json.loads(encoded))
            except (ValueError, UnicodeError, RecursionError):
                decoded = None
            if decoded is None:
                return json.dumps({
                    "error": "saved result has no text body; use view:raw for its metadata or skill.content for a needed K2 body",
                })
            body, body_field = decoded
            body_bytes = body.encode("utf-8")
            total_bytes = len(body_bytes)
        if offset > total_bytes:
            return json.dumps({"error": "offset exceeds the saved result size"})
        if view == "body":
            raw_page = body_bytes[offset:offset + limit + 4]
        else:
            with path.open("rb") as handle:
                handle.seek(offset)
                raw_page = handle.read(limit + 4)
    except (OSError, UnicodeError):
        return json.dumps({"error": "saved result could not be read"})

    # Raw pages seek directly; body pages use byte cursors within the bounded
    # decoded envelope. Extend by at most three bytes to finish the final UTF-8
    # code point. Reject caller-chosen offsets inside a code point.
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
            **({"view": "body", "bodyField": body_field, "sourceBytes": source_bytes} if view == "body" else {}),
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


def _slack_thread_ts(raw_message: Mapping[str, Any], message_ts: str) -> str:
    """Return Slack's durable thread key, including a top-level starter."""
    return str(raw_message.get("thread_ts") or message_ts or "").strip()


def _bare_transfer_rewrite(
    event: Any, *, selected_agent_ref: str | None
) -> dict[str, Any] | None:
    """Recover a real thread task when the transfer message is only ``@agent``.

    The Slack adapter strips its own mention before this hook runs. A bare
    transfer therefore arrives with empty ``event.text`` but with an
    authenticated, bounded ``channel_context`` fetched from
    ``conversations.replies``. Promote that exact context into the durable user
    message so K2 sees the actual mission, transcript compaction cannot erase
    it during this turn, and every provider retry receives the same request.
    ``channel_context`` is cleared in the rewrite to avoid duplicating it at
    the gateway's normal prepend step.
    """
    if str(getattr(event, "text", "") or "").strip():
        return None
    context = str(getattr(event, "channel_context", "") or "").strip()
    if not context:
        return None
    agent_name = str(selected_agent_ref or "the selected agent").removeprefix(
        "agent:"
    )
    recovered = (
        f"{context}\n\n"
        "[Ownership transfer]\n"
        f"The latest verified user explicitly transferred this thread to "
        f"{agent_name}. Complete the substantive request in the thread context."
    )
    return {"action": "rewrite", "text": recovered, "channel_context": ""}


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


def _pre_gateway_dispatch(event: Any = None, **_: Any) -> dict[str, Any] | None:
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
        workspace_id, channel_id, message_ts = _slack_key(event, raw_message)
        thread_ts = _slack_thread_ts(raw_message, message_ts)
        ledger = _lead_ledger()
        thread_participants = ledger.thread_participants(
            workspace_id=workspace_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        decision = select_slack_agent_lead(
            raw_message,
            local_agent_ref=local_agent_ref,
            roster=roster,
            thread_participant_agent_refs=thread_participants,
        )
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
        if (
            decision.recognized_mentions
            and is_human_authored_message(raw_message, roster)
            and decision.reason != "edited_message_frozen"
        ):
            # Every runtime observes the same explicit human invitation,
            # including agents that suppress this turn. Persisting the full set
            # on each disk keeps every invited specialist conversational after
            # a restart. A later human mention replaces/narrows this set.
            ledger.assign_thread_participants(
                workspace_id=workspace_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                agent_refs=decision.recognized_mentions,
                mention_message_ts=message_ts,
            )
        tombstone = ledger.record_once(
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
        if decision.recognized_mentions:
            recovered = _bare_transfer_rewrite(
                event, selected_agent_ref=decision.selected_agent_ref
            )
            if recovered is not None:
                return recovered
        # None means normal dispatch without short-circuiting a later policy
        # hook; Hermes stops evaluating hooks after an explicit allow result.
        return None
    return {"action": "skip", "reason": decision.reason}


def _pre_llm_call(
    user_message: str = "",
    platform: str = "",
    session_id: str = "",
    turn_id: str = "",
    scheduled_run_budget: bool = False,
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
    if scheduled_run_budget:
        # This opt-in native job already names its one K2 read. An automatic
        # well draw would spend outside its provider budget before the first
        # model call. Ordinary Slack/cron mission discovery is unchanged.
        return {"context": "Bounded daily K2 check: use the supplied record directly; do not start a wishing-well draw."}
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
        "optional byte offset and limit. For a saved K2 JSON envelope, choose "
        "view:body to read decoded body text instead of escaped one-line JSON; "
        "follow nextOffset using the same view. Available to Slack and API runs. "
        "Read-only and session/spillover-scoped; no shell or code execution needed."
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
                    "view": {
                        "type": "string",
                        "enum": ["raw", "body"],
                        "default": "raw",
                        "description": "raw pages the saved UTF-8 bytes; body decodes a K2 envelope and pages body_md/body/markdown bytes (source at most 1 MiB). Keep the same view between pages.",
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
