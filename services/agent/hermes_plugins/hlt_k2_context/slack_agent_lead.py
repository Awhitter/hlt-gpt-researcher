"""Deterministic participation for Slack messages addressed to HLT agents.

The selector in this module is deliberately pure: it reads only Slack's raw
message payload plus a ready roster and returns a decision.  Durable replay
suppression and private logging live in the plugin integration, not here.

The four-row fallback is a generated, hash-pinned projection of the current
HLT Slack agent roster.  Katailyst2 is the future canonical projection source;
until that typed contract exists, changing any row requires changing the
digest and the cross-runtime contract tests together.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ROSTER_FALLBACK_ROWS: tuple[dict[str, str], ...] = (
    {
        "agent_ref": "agent:victoria",
        "name": "victoria",
        "slack_user_id": "U0AHLTX283E",
    },
    {
        "agent_ref": "agent:lila",
        "name": "lila",
        "slack_user_id": "U0AHF34M006",
    },
    {
        "agent_ref": "agent:julius",
        "name": "julius",
        "slack_user_id": "U0AH1TNC3P1",
    },
    {
        "agent_ref": "agent:cleo",
        "name": "cleo",
        "slack_user_id": "U0BM3ULM210",
    },
)
ROSTER_FALLBACK_SHA256 = (
    "b6dc04388d03d378bfffe1d89be428ea1c8394a9e0e54230091d22e3b9777ec5"
)
ROSTER_PARTICIPANT_REFS = frozenset(
    {"agent:victoria", "agent:lila", "agent:julius", "agent:cleo"}
)
ROSTER_NONPARTICIPANT_REFS = frozenset({"agent:brian"})
_SLACK_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>", re.IGNORECASE)


@dataclass(frozen=True)
class AgentRosterEntry:
    agent_ref: str
    name: str
    slack_user_id: str


@dataclass(frozen=True)
class AgentRoster:
    entries: tuple[AgentRosterEntry, ...]
    sha256: str
    source: str
    ready: bool
    error: str = ""

    @property
    def by_user_id(self) -> dict[str, AgentRosterEntry]:
        return {entry.slack_user_id: entry for entry in self.entries}

    @property
    def by_agent_ref(self) -> dict[str, AgentRosterEntry]:
        return {entry.agent_ref: entry for entry in self.entries}


@dataclass(frozen=True)
class SlackLeadDecision:
    action: str
    reason: str
    channel_kind: str
    local_agent_ref: str
    selected_agent_ref: str | None
    recognized_mentions: tuple[str, ...]
    roster_sha256: str

    @property
    def allows_dispatch(self) -> bool:
        return self.action == "allow"


def _canonical_roster_payload(rows: Sequence[Mapping[str, str]]) -> str:
    return json.dumps(list(rows), sort_keys=True, separators=(",", ":"))


def load_fallback_roster() -> AgentRoster:
    """Load the pinned local roster, failing closed on any drift."""
    digest = hashlib.sha256(
        _canonical_roster_payload(ROSTER_FALLBACK_ROWS).encode("utf-8")
    ).hexdigest()
    entries = tuple(
        AgentRosterEntry(
            agent_ref=str(row.get("agent_ref") or "").strip(),
            name=str(row.get("name") or "").strip().lower(),
            slack_user_id=str(row.get("slack_user_id") or "").strip().upper(),
        )
        for row in ROSTER_FALLBACK_ROWS
    )
    refs = [entry.agent_ref for entry in entries]
    user_ids = [entry.slack_user_id for entry in entries]
    valid = (
        digest == ROSTER_FALLBACK_SHA256
        and len(entries) == 4
        and set(refs) == ROSTER_PARTICIPANT_REFS
        and len(set(refs)) == len(refs)
        and len(set(user_ids)) == len(user_ids)
        and all(ref.startswith("agent:") for ref in refs)
        and all(re.fullmatch(r"U[A-Z0-9]{8,}", user_id) for user_id in user_ids)
    )
    return AgentRoster(
        entries=entries,
        sha256=digest,
        source="generated_fallback_future_katailyst2_projection",
        ready=valid,
        error="" if valid else "fallback roster digest or shape mismatch",
    )


def _mentions_from_text(text: str, known_ids: set[str]) -> list[str]:
    """Read authored Slack text while ignoring quote and fenced-code lines."""
    found: list[str] = []
    in_fence = False
    for line in str(text or "").splitlines() or [str(text or "")]:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            # A line that both opens and closes its fence (```df -h```) must
            # not flip the tracker: one such line would otherwise mark every
            # later line as fenced and silently swallow the mention after it.
            if not (len(stripped) > 3 and stripped.rstrip().endswith("```")):
                in_fence = not in_fence
            continue
        if in_fence or stripped.startswith(">"):
            continue
        for match in _SLACK_MENTION.finditer(line):
            user_id = match.group(1).upper()
            if user_id in known_ids:
                found.append(user_id)
    return found


def _mentions_from_rich_node(node: Any, known_ids: set[str]) -> list[str]:
    if not isinstance(node, Mapping):
        return []
    node_type = str(node.get("type") or "")
    if node_type in {"rich_text_quote", "rich_text_preformatted"}:
        return []
    if node_type == "user":
        user_id = str(node.get("user_id") or node.get("user") or "").upper()
        return [user_id] if user_id in known_ids else []

    found: list[str] = []
    text = node.get("text")
    if isinstance(text, str):
        found.extend(_mentions_from_text(text, known_ids))
    elements = node.get("elements")
    if isinstance(elements, list):
        for element in elements:
            found.extend(_mentions_from_rich_node(element, known_ids))
    return found


def recognized_mentions(
    raw_message: Mapping[str, Any], roster: AgentRoster
) -> tuple[str, ...]:
    """Return recognized agent refs in authored, nonquoted mention order."""
    by_user_id = roster.by_user_id
    known_ids = set(by_user_id)
    blocks = raw_message.get("blocks")
    structured = False
    user_ids: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, Mapping) and block.get("type") == "rich_text":
                structured = True
                user_ids.extend(_mentions_from_rich_node(block, known_ids))
    if not structured:
        user_ids = _mentions_from_text(str(raw_message.get("text") or ""), known_ids)
    return tuple(by_user_id[user_id].agent_ref for user_id in user_ids)


def _channel_kind(raw_message: Mapping[str, Any]) -> str:
    channel_type = str(raw_message.get("channel_type") or "").strip().lower()
    if channel_type == "im":
        return "dm"
    if channel_type == "mpim":
        return "mpim"
    channel_id = str(raw_message.get("channel") or "").strip().upper()
    if not channel_type and channel_id.startswith("D"):
        return "dm"
    return "shared"


def _is_edit(raw_message: Mapping[str, Any]) -> bool:
    return (
        bool(raw_message.get("_slack_changed_event_ts"))
        or str(raw_message.get("subtype") or "") == "message_changed"
    )


def _has_explicit_bot_shape(raw_message: Mapping[str, Any]) -> bool:
    if raw_message.get("bot_id") or raw_message.get("bot_profile"):
        return True
    if str(raw_message.get("subtype") or "") == "bot_message":
        return True
    profile = raw_message.get("user_profile")
    return isinstance(profile, Mapping) and bool(profile.get("is_bot"))


def _is_verified_app_mediated_human(raw_message: Mapping[str, Any]) -> bool:
    """Recognize a human-authored installed-app message without trusting app id."""
    return bool(
        str(raw_message.get("user") or "").strip()
        and raw_message.get("app_id")
        and not _has_explicit_bot_shape(raw_message)
        and raw_message.get("_hermes_verified_human_app_relay")
    )


def _is_bot_sender(raw_message: Mapping[str, Any]) -> bool:
    # Explicit Slack bot identity always wins. By contrast, installed apps can
    # post a message Slack still attributes to its human author; Hermes' cached
    # users.info marker is advisory for that shape and must not turn the human
    # into an unknown bot.
    if _has_explicit_bot_shape(raw_message):
        return True
    if _is_verified_app_mediated_human(raw_message):
        return False
    if raw_message.get("app_id"):
        return True
    return bool(raw_message.get("_hermes_sender_is_bot"))


def select_slack_agent_lead(
    raw_message: Mapping[str, Any] | Any,
    *,
    local_agent_ref: str,
    roster: AgentRoster,
    thread_participant_agent_refs: Sequence[str] = (),
) -> SlackLeadDecision:
    """Choose one local outcome for a normalized raw Slack message.

    One-to-one human DMs belong to the DM'd agent. In shared surfaces, the
    fresh nonquoted mention invites every named agent; unmentioned agents stay
    silent. That human-invited participant set remains conversational for later
    unmentioned human follow-ups until another explicit human mention replaces
    it. Bot replies never inherit or expand participation, and peers must still
    explicitly mention one target. Edits never reopen a frozen decision.
    """

    local_ref = str(local_agent_ref or "").strip().lower()
    participant_refs = tuple(
        dict.fromkeys(
            str(value or "").strip().lower()
            for value in thread_participant_agent_refs
        )
    )
    if not isinstance(raw_message, Mapping):
        raw_message = {}
    kind = _channel_kind(raw_message)
    mentions = recognized_mentions(raw_message, roster) if roster.ready else ()
    base = {
        "channel_kind": kind,
        "local_agent_ref": local_ref,
        "selected_agent_ref": mentions[0] if mentions else None,
        "recognized_mentions": mentions,
        "roster_sha256": roster.sha256,
    }

    if not roster.ready or local_ref not in roster.by_agent_ref:
        return SlackLeadDecision(action="suppress", reason="roster_unready", **base)
    if _is_edit(raw_message):
        return SlackLeadDecision(
            action="suppress", reason="edited_message_frozen", **base
        )

    sender_user_id = str(raw_message.get("user") or "").strip().upper()
    sender_is_recognized_peer = sender_user_id in roster.by_user_id
    # Hermes can identify a peer through users.info even when Slack omits the
    # bot_id/subtype markers from the raw event. The pinned roster user IDs are
    # bot identities, so they remain peers at this pure-selector boundary.
    sender_is_bot = _is_bot_sender(raw_message) or sender_is_recognized_peer
    if (
        sender_is_recognized_peer
        and roster.by_user_id[sender_user_id].agent_ref == local_ref
    ):
        return SlackLeadDecision(action="suppress", reason="self_bot_sender", **base)
    if sender_is_bot and not sender_is_recognized_peer:
        return SlackLeadDecision(
            action="suppress", reason="unrecognized_bot_sender", **base
        )
    if sender_is_bot:
        if not mentions:
            return SlackLeadDecision(
                action="suppress", reason="peer_request_requires_mention", **base
            )
        if mentions[0] != local_ref:
            return SlackLeadDecision(
                action="suppress", reason="another_agent_mentioned_first", **base
            )
        return SlackLeadDecision(action="allow", reason="explicit_peer_request", **base)

    if kind == "dm":
        return SlackLeadDecision(action="allow", reason="owned_dm", **base)
    if not mentions:
        valid_participants = tuple(
            value for value in participant_refs if value in roster.by_agent_ref
        )
        if valid_participants:
            return SlackLeadDecision(
                action=("allow" if local_ref in valid_participants else "suppress"),
                reason=(
                    "thread_participant_continuation"
                    if local_ref in valid_participants
                    else "not_a_thread_participant"
                ),
                channel_kind=kind,
                local_agent_ref=local_ref,
                selected_agent_ref=(
                    local_ref if local_ref in valid_participants else valid_participants[0]
                ),
                recognized_mentions=mentions,
                roster_sha256=roster.sha256,
            )
        return SlackLeadDecision(
            action="suppress", reason="shared_surface_requires_fresh_mention", **base
        )
    if local_ref in mentions:
        return SlackLeadDecision(
            action="allow",
            reason=(
                "first_recognized_mention"
                if mentions[0] == local_ref
                else "explicit_human_invitation"
            ),
            channel_kind=kind,
            local_agent_ref=local_ref,
            selected_agent_ref=local_ref,
            recognized_mentions=mentions,
            roster_sha256=roster.sha256,
        )
    return SlackLeadDecision(
        action="suppress", reason="another_agent_mentioned_first", **base
    )
