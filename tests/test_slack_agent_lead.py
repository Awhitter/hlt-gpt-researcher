from __future__ import annotations

import importlib.util
import json
import logging
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "services" / "agent" / "hermes_plugins" / "hlt_k2_context"


def _load_plugin():
    name = "hlt_k2_context_lead_test"
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(name, None)


def _load_submodule(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(name, None)


@pytest.fixture
def lead():
    return _load_submodule("slack_agent_lead.py", "slack_agent_lead_test")


@pytest.fixture
def ledger_module():
    return _load_submodule("slack_lead_ledger.py", "slack_lead_ledger_test")


def _raw(
    text: str = "",
    *,
    channel_type: str = "channel",
    channel: str = "C0BNVFN5MM5",
    ts: str = "1787141352.524009",
    user: str = "U0AL2GDUA7U",
    **extra,
):
    return {
        "type": "message",
        "team": "T_HLT",
        "channel": channel,
        "channel_type": channel_type,
        "ts": ts,
        "user": user,
        "client_msg_id": f"client-{ts}",
        "text": text,
        **extra,
    }


def _rich(*elements):
    return [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": list(elements),
                }
            ],
        }
    ]


def _decision(lead, raw, local="agent:cleo", roster=None):
    return lead.select_slack_agent_lead(
        raw,
        local_agent_ref=local,
        roster=roster or lead.load_fallback_roster(),
    )


def _event(raw, *, platform="slack"):
    return SimpleNamespace(
        raw_message=raw,
        message_id=str(raw.get("ts") or ""),
        metadata={
            "slack_team_id": str(raw.get("team") or "T_HLT"),
            "slack_channel_id": str(raw.get("channel") or "C0BNVFN5MM5"),
        },
        source=SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            scope_id=str(raw.get("team") or "T_HLT"),
            chat_id=str(raw.get("channel") or "C0BNVFN5MM5"),
        ),
    )


def test_hash_pinned_fallback_contains_the_four_live_agents(lead):
    roster = lead.load_fallback_roster()

    assert roster.ready is True
    assert roster.source == "generated_fallback_future_katailyst2_projection"
    assert roster.sha256 == lead.ROSTER_FALLBACK_SHA256
    assert {entry.agent_ref: entry.slack_user_id for entry in roster.entries} == {
        "agent:victoria": "U0AHLTX283E",
        "agent:lila": "U0AHF34M006",
        "agent:julius": "U0AH1TNC3P1",
        "agent:cleo": "U0BM3ULM210",
    }


def test_roster_drift_fails_closed(lead, monkeypatch):
    monkeypatch.setattr(lead, "ROSTER_FALLBACK_SHA256", "0" * 64)
    roster = lead.load_fallback_roster()
    decision = _decision(
        lead,
        _raw("<@U0BM3ULM210> answer this"),
        roster=roster,
    )

    assert roster.ready is False
    assert decision.action == "suppress"
    assert decision.reason == "roster_unready"


def test_human_one_to_one_dm_is_owned_without_a_mention(lead):
    decision = _decision(
        lead,
        _raw("Can you check the funnel?", channel_type="im", channel="D123456789"),
    )

    assert decision.action == "allow"
    assert decision.reason == "owned_dm"
    assert decision.channel_kind == "dm"


def test_mpim_is_shared_and_requires_a_fresh_mention(lead):
    silent = _decision(
        lead,
        _raw("Can someone check this?", channel_type="mpim", channel="G123456789"),
    )
    addressed = _decision(
        lead,
        _raw(
            "<@U0BM3ULM210> can you check this?",
            channel_type="mpim",
            channel="G123456789",
        ),
    )

    assert silent.action == "suppress"
    assert silent.reason == "shared_surface_requires_fresh_mention"
    assert addressed.action == "allow"
    assert addressed.channel_kind == "mpim"


def test_first_recognized_native_mention_wins_for_every_runtime(lead):
    raw = _raw("<@U0AHLTX283E> and <@U0BM3ULM210> dig into this")

    victoria = _decision(lead, raw, local="agent:victoria")
    cleo = _decision(lead, raw, local="agent:cleo")

    assert victoria.action == "allow"
    assert victoria.selected_agent_ref == "agent:victoria"
    assert cleo.action == "suppress"
    assert cleo.reason == "another_agent_mentioned_first"
    assert cleo.recognized_mentions == ("agent:victoria", "agent:cleo")


def test_structured_quote_mentions_do_not_win(lead):
    raw = _raw(
        "<@U0BM3ULM210> take this",
        blocks=[
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_quote",
                        "elements": [
                            {"type": "user", "user_id": "U0AHLTX283E"},
                            {"type": "text", "text": " old request"},
                        ],
                    },
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "user", "user_id": "U0BM3ULM210"},
                            {"type": "text", "text": " take this"},
                        ],
                    },
                ],
            }
        ],
    )

    decision = _decision(lead, raw)

    assert decision.action == "allow"
    assert decision.recognized_mentions == ("agent:cleo",)


def test_structured_preformatted_mentions_do_not_win(lead):
    raw = _raw(
        "<@U0BM3ULM210> take this",
        blocks=[
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_preformatted",
                        "elements": [
                            {"type": "user", "user_id": "U0AHLTX283E"},
                            {"type": "text", "text": " example only"},
                        ],
                    },
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "user", "user_id": "U0BM3ULM210"},
                            {"type": "text", "text": " take this"},
                        ],
                    },
                ],
            }
        ],
    )

    decision = _decision(lead, raw)

    assert decision.action == "allow"
    assert decision.recognized_mentions == ("agent:cleo",)


def test_plaintext_quote_and_fenced_example_mentions_do_not_win(lead):
    raw = _raw(
        "> <@U0AHLTX283E> old quoted request\n"
        "```\n<@U0AHF34M006> example only\n```\n"
        "<@U0BM3ULM210> take the live request"
    )

    decision = _decision(lead, raw)

    assert decision.action == "allow"
    assert decision.recognized_mentions == ("agent:cleo",)


def test_rich_text_native_user_order_beats_flat_text_order(lead):
    raw = _raw(
        "<@U0AHLTX283E> stale flat rendering <@U0BM3ULM210>",
        blocks=_rich(
            {"type": "user", "user_id": "U0BM3ULM210"},
            {"type": "text", "text": " first in authored rich text, then "},
            {"type": "user", "user_id": "U0AHLTX283E"},
        ),
    )

    decision = _decision(lead, raw)

    assert decision.action == "allow"
    assert decision.recognized_mentions == ("agent:cleo", "agent:victoria")


def test_message_edits_never_reopen_lead_selection_even_in_a_dm(lead):
    decision = _decision(
        lead,
        _raw(
            "<@U0BM3ULM210> added after the fact",
            channel_type="im",
            channel="D123456789",
            _slack_changed_event_ts="1787141353.000100",
        ),
    )

    assert decision.action == "suppress"
    assert decision.reason == "edited_message_frozen"


def test_recognized_bot_peer_must_explicitly_mention_the_target(lead):
    no_handoff = _decision(
        lead,
        _raw(
            "I finished my task",
            user="U0AHLTX283E",
            bot_id="BVICTORIA",
            client_msg_id="",
        ),
    )
    handoff = _decision(
        lead,
        _raw(
            "<@U0BM3ULM210> inspect this bounded packet",
            user="U0AHLTX283E",
            bot_id="BVICTORIA",
            client_msg_id="",
        ),
    )

    assert no_handoff.action == "suppress"
    assert no_handoff.reason == "peer_request_requires_mention"
    assert handoff.action == "allow"
    assert handoff.reason == "explicit_peer_request"


def test_recognized_peer_user_id_stays_a_bot_without_raw_bot_markers(lead):
    decision = _decision(
        lead,
        _raw(
            "I finished my task",
            channel_type="im",
            channel="D123456789",
            user="U0AHLTX283E",
            client_msg_id="",
        ),
    )

    assert decision.action == "suppress"
    assert decision.reason == "peer_request_requires_mention"


def test_unrecognized_bot_cannot_dispatch_even_when_it_mentions_cleo(lead):
    decision = _decision(
        lead,
        _raw(
            "<@U0BM3ULM210> run this",
            user="UUNRECOGNIZED1",
            bot_id="BUNKNOWN",
            client_msg_id="",
        ),
    )

    assert decision.action == "suppress"
    assert decision.reason == "unrecognized_bot_sender"


def test_adapter_resolved_markerless_bot_cannot_dispatch(lead):
    decision = _decision(
        lead,
        _raw(
            "<@U0BM3ULM210> run this",
            user="UUNRECOGNIZED1",
            client_msg_id="",
            _hermes_sender_is_bot=True,
        ),
    )

    assert decision.action == "suppress"
    assert decision.reason == "unrecognized_bot_sender"


def test_selector_is_pure_and_does_not_mutate_slack_payload(lead):
    raw = _raw(
        "<@U0BM3ULM210> go",
        blocks=_rich({"type": "user", "user_id": "U0BM3ULM210"}),
    )
    before = deepcopy(raw)

    _decision(lead, raw)

    assert raw == before


def test_sqlite_tombstone_is_atomic_across_concurrent_retries(ledger_module, tmp_path):
    path = tmp_path / "lead.sqlite3"
    receipt = {
        "schema": ledger_module.RECEIPT_SCHEMA,
        "action": "allow",
        "reason": "first_recognized_mention",
    }
    # Initialize schema before the concurrency probe; concurrent work here is
    # the decision insert itself, matching Socket Mode retry behavior.
    ledger_module.SlackLeadLedger(path)._connect().close()

    def record():
        return (
            ledger_module.SlackLeadLedger(path)
            .record_once(
                workspace_id="T_HLT",
                channel_id="C0BNVFN5MM5",
                message_ts="1787141352.524009",
                receipt=receipt,
            )
            .inserted
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        inserted = list(pool.map(lambda _: record(), range(8)))

    assert inserted.count(True) == 1
    assert inserted.count(False) == 7


def test_hook_records_private_receipt_then_suppresses_restart_replay(
    monkeypatch, tmp_path, caplog
):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HLT_AGENT_REF", "agent:cleo")
    secret_message = "<@U0BM3ULM210> confidential funnel question"
    event = _event(_raw(secret_message))

    with caplog.at_level(logging.INFO):
        first = plugin._pre_gateway_dispatch(event=event)
        replay = plugin._pre_gateway_dispatch(event=event)

    assert first is None
    assert replay == {"action": "skip", "reason": "durable_replay_tombstone"}

    connection = sqlite3.connect(tmp_path / "slack-agent-lead.sqlite3")
    stored = connection.execute(
        "SELECT receipt_json FROM slack_agent_lead_tombstones"
    ).fetchall()
    connection.close()
    assert len(stored) == 1
    receipt = json.loads(stored[0][0])
    assert receipt["schema"] == "slack_agent_lead_decision.v1"
    assert receipt["selectedAgentRef"] == "agent:cleo"
    assert receipt["action"] == "allow"
    assert secret_message not in json.dumps(receipt)
    assert secret_message not in caplog.text


def test_valid_json_corruption_in_replay_tombstone_fails_closed(monkeypatch, tmp_path):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HLT_AGENT_REF", "agent:cleo")
    event = _event(_raw("<@U0BM3ULM210> answer"))
    assert plugin._pre_gateway_dispatch(event=event) is None

    connection = sqlite3.connect(tmp_path / "slack-agent-lead.sqlite3")
    connection.execute(
        "UPDATE slack_agent_lead_tombstones SET receipt_json = ?",
        ("[]",),
    )
    connection.commit()
    connection.close()

    assert plugin._pre_gateway_dispatch(event=event) == {
        "action": "skip",
        "reason": "lead_ledger_unavailable",
    }


def test_hook_fails_closed_when_durable_key_is_unavailable(monkeypatch, tmp_path):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HLT_AGENT_REF", "agent:cleo")
    raw = _raw("<@U0BM3ULM210> answer", channel="", ts="")
    raw["team"] = ""
    event = _event(raw)
    event.metadata = {}
    event.message_id = ""
    event.source.scope_id = ""
    event.source.chat_id = ""

    result = plugin._pre_gateway_dispatch(event=event)

    assert result == {"action": "skip", "reason": "lead_ledger_unavailable"}


def test_hook_fails_closed_when_selector_or_roster_loading_raises(
    monkeypatch, tmp_path
):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HLT_AGENT_REF", "agent:cleo")
    monkeypatch.setattr(
        plugin,
        "load_fallback_roster",
        lambda: (_ for _ in ()).throw(RuntimeError("unexpected roster fault")),
    )

    result = plugin._pre_gateway_dispatch(event=_event(_raw("<@U0BM3ULM210> answer")))

    assert result == {"action": "skip", "reason": "lead_selection_unavailable"}


@pytest.mark.parametrize(
    "raw,message_type",
    [
        (
            {
                "command": "/stop",
                "text": "",
                "team_id": "T_HLT",
                "channel_id": "C0BNVFN5MM5",
                "user_id": "U0AL2GDUA7U",
                "trigger_id": "1787141352.123456.abcdef",
            },
            "command",
        ),
        (_raw("!approve"), "command"),
    ],
)
def test_local_slack_commands_bypass_lead_arbitration(
    monkeypatch, tmp_path, raw, message_type
):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HLT_AGENT_REF", "agent:cleo")
    event = _event(raw)
    event.message_type = SimpleNamespace(value=message_type)

    assert plugin._pre_gateway_dispatch(event=event) is None
    assert not (tmp_path / "slack-agent-lead.sqlite3").exists()


def test_edited_message_form_command_stays_frozen(monkeypatch, tmp_path):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HLT_AGENT_REF", "agent:cleo")
    event = _event(
        _raw(
            "!stop <@U0BM3ULM210>",
            _slack_changed_event_ts="1787141353.000100",
        )
    )
    event.message_type = SimpleNamespace(value="command")

    assert plugin._pre_gateway_dispatch(event=event) == {
        "action": "skip",
        "reason": "edited_message_frozen",
    }
    assert (tmp_path / "slack-agent-lead.sqlite3").is_file()


def test_nonparticipant_runtime_is_untouched(monkeypatch, tmp_path):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HLT_AGENT_REF", "agent:brian")

    assert plugin._pre_gateway_dispatch(event=_event(_raw("hello"))) is None
    assert not (tmp_path / "slack-agent-lead.sqlite3").exists()


def test_unknown_runtime_ref_fails_closed_instead_of_bypassing(monkeypatch, tmp_path):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HLT_AGENT_REF", "agent:clep")

    result = plugin._pre_gateway_dispatch(
        event=_event(_raw("<@U0BM3ULM210> answer"))
    )

    assert result == {"action": "skip", "reason": "roster_unready"}
    assert (tmp_path / "slack-agent-lead.sqlite3").is_file()


def test_non_slack_events_are_untouched(monkeypatch, tmp_path):
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert (
        plugin._pre_gateway_dispatch(event=_event(_raw("hello"), platform="telegram"))
        is None
    )
    assert not (tmp_path / "slack-agent-lead.sqlite3").exists()


def test_plugin_registers_lead_selection_before_mission_context():
    plugin = _load_plugin()
    registrations = []
    context = SimpleNamespace(
        register_hook=lambda name, callback: registrations.append((name, callback))
    )

    plugin.register(context)

    assert [name for name, _ in registrations] == [
        "pre_gateway_dispatch",
        "pre_llm_call",
    ]


def test_manifest_and_image_pin_the_supported_pretyping_hook():
    manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "services" / "agent" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    patch = (
        ROOT
        / "services"
        / "agent"
        / "hermes_patches"
        / "pre_gateway_dispatch_before_typing.patch"
    ).read_text(encoding="utf-8")

    assert "- pre_gateway_dispatch" in manifest
    assert "hermes_cli/plugins.py" in dockerfile
    assert "pre_gateway_dispatch skip" in dockerfile
    assert "ARG HERMES_REF=83a1ca686207ef797e4eb86a46725dfe7d9a2f10" in dockerfile
    assert "git -C /opt/hermes apply --check" in dockerfile
    assert "assert_pre_gateway_dispatch.py /opt/hermes" in dockerfile
    assert patch.count("diff --git") == 3
    assert "skip before adapter processing" in patch
    assert "_hermes_pre_gateway_dispatch_done" in patch
    assert "_hermes_sender_is_bot" in patch


def test_single_line_fence_does_not_swallow_the_mention_after_it(lead):
    """A self-closing fence line (```df -h```) must not flip the fence
    tracker: with an odd toggle every later line reads as fenced and the
    mention after it is silently dropped — the turn suppresses as
    shared_surface_requires_fresh_mention with a durable tombstone."""
    raw = _raw("```df -h```\n<@U0AHLTX283E> what does this mean?")

    victoria = _decision(lead, raw, local="agent:victoria")

    assert victoria.action == "allow"
    assert victoria.selected_agent_ref == "agent:victoria"


def test_multiline_fences_still_hide_their_contents(lead):
    raw = _raw("```\n<@U0BM3ULM210> quoted inside code\n```\n<@U0AHLTX283E> real ask")

    victoria = _decision(lead, raw, local="agent:victoria")
    cleo = _decision(lead, raw, local="agent:cleo")

    assert victoria.action == "allow"
    assert cleo.action == "suppress"
    assert cleo.recognized_mentions == ("agent:victoria",)
