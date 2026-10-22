"""Cleo reports each finished Slack/API reply to Katailyst2 (agents.run.record)."""
from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PLUGIN_DIR = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "agent"
    / "hermes_plugins"
    / "hlt_k2_context"
)
PACKAGE = "hlt_k2_context_reply_test"
NOW = datetime(2026, 10, 17, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def plugin():
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    yield module
    for name in [name for name in sys.modules if name.startswith(PACKAGE)]:
        sys.modules.pop(name, None)


@pytest.fixture()
def outcome(plugin):
    module = sys.modules[f"{PACKAGE}.reply_outcome"]
    module._reset_for_tests()
    return module


def test_a_finished_slack_reply_becomes_one_completed_run(outcome):
    outcome.note_turn_started("turn-1", now=100.0)

    record = outcome.reply_outcome_record(
        "agent:cleo",
        session_id="slack:C1:171",
        turn_id="turn-1",
        completed=True,
        turn_exit_reason="text_response(finish_reason=stop)",
        model="openai/gpt-6-astra",
        platform="slack",
        monotonic_now=112.0,
        now=NOW,
    )

    assert record == {
        "agentRef": "agent:cleo",
        "mission": "Reply on slack",
        "outcome": "completed",
        "runtime": "hermes",
        "summary": "Replied in 12s on openai/gpt-6-astra.",
        "externalRef": "slack:C1:171",
        "completedAt": "2026-10-17T20:00:00Z",
        "startedAt": "2026-10-17T19:59:48Z",
        "idempotencyKey": "turn:turn-1",
    }


def test_a_failed_api_turn_keeps_the_error_line_without_key_shaped_strings(outcome):
    record = outcome.reply_outcome_record(
        "agent:cleo",
        session_id="hook:k2:run-9",
        turn_id="turn-2",
        failed=True,
        turn_exit_reason="local_processing_error(401 Bearer sk-or-v1-abcdef123456 rejected)",
        model="gpt-5.6-sol",
        platform="api_server",
        now=NOW,
    )

    assert record["mission"] == "Reply on api"
    assert record["outcome"] == "failed"
    assert record["externalRef"] == "hook:k2:run-9"
    assert "sk-or" not in record["summary"]
    assert record["summary"].startswith("Reply failed on gpt-5.6-sol: local_processing_error(401 [redacted]")
    assert "startedAt" not in record


def test_a_turn_cap_summary_is_a_delivered_reply(outcome):
    record = outcome.reply_outcome_record(
        "agent:cleo", turn_id="t", completed=False, failed=False,
        turn_exit_reason="max_iterations_reached(7/7)", model="m", platform="slack", now=NOW,
    )

    assert record["outcome"] == "completed"
    assert record["summary"] == "Replied on m (summary at the turn cap)."


def test_a_turn_that_ended_without_an_answer_is_a_failure(outcome):
    record = outcome.reply_outcome_record(
        "agent:cleo", turn_id="t", completed=False, failed=False,
        turn_exit_reason="empty_response_exhausted", platform="slack", now=NOW,
    )

    assert record["outcome"] == "failed"
    assert "empty_response_exhausted" in record["summary"]


@pytest.mark.parametrize(
    "turn",
    [
        {"platform": "cron", "turn_id": "t", "completed": True},
        {"platform": "subagent", "turn_id": "t", "completed": True},
        {"platform": "", "turn_id": "t", "completed": True},
        {"platform": "slack", "turn_id": "t", "interrupted": True},
        {"platform": "slack", "turn_id": "", "completed": True},
    ],
)
def test_housekeeping_subagents_and_interrupted_turns_are_not_replies(outcome, turn):
    assert outcome.reply_outcome_record("agent:cleo", **turn) is None


def test_reporting_failure_is_one_warning_and_never_raises(outcome, caplog):
    def send(*_):
        raise RuntimeError("K2 down; token sk-live-0123456789abcdef")

    caplog.set_level(logging.WARNING)
    thread = outcome.report_in_background(
        "https://k2.example/mcp", "token", {"outcome": "completed"}, send=send
    )
    thread.join(timeout=2)

    assert "could not report a completed reply" in caplog.text
    assert "sk-live" not in caplog.text
    assert outcome.report_in_background("", "token", {}, send=send) is None


def test_record_run_calls_agents_run_record_once(outcome, monkeypatch):
    sent = []

    def post(url, token, payload, *, session_id="", timeout=0):
        sent.append(payload)
        if payload["method"] == "initialize":
            return {"result": {}}, "sess-1", {}
        return {"result": {"structuredContent": {"recorded": True}}}, session_id, {}

    monkeypatch.setattr(outcome, "_post", post)
    outcome.record_run("https://k2.example/mcp", "token", {"agentRef": "agent:cleo"})

    assert [item["method"] for item in sent] == ["initialize", "tools/call"]
    assert sent[1]["params"] == {
        "name": "agents.run.record",
        "arguments": {"agentRef": "agent:cleo"},
    }


def test_record_run_tries_the_wire_alias_only_for_an_unknown_name(outcome, monkeypatch):
    names = []

    def post(url, token, payload, *, session_id="", timeout=0):
        if payload["method"] == "initialize":
            return {"result": {}}, "sess-1", {}
        names.append(payload["params"]["name"])
        if payload["params"]["name"] == "agents.run.record":
            return {"error": {"message": "Tool not found"}}, session_id, {}
        return {"result": {}}, session_id, {}

    monkeypatch.setattr(outcome, "_post", post)
    outcome.record_run("u", "t", {})
    assert names == ["agents.run.record", "agents_run_record"]

    names.clear()

    def refusing(url, token, payload, *, session_id="", timeout=0):
        if payload["method"] == "initialize":
            return {"result": {}}, "sess-1", {}
        names.append(payload["params"]["name"])
        return {"result": {"isError": True, "content": "forbidden"}}, session_id, {}

    monkeypatch.setattr(outcome, "_post", refusing)
    with pytest.raises(RuntimeError):
        outcome.record_run("u", "t", {})
    assert names == ["agents.run.record"]


def test_usage_from_two_provider_calls_is_sent_once_with_the_turn(plugin, outcome):
    # The shape Hermes' _usage_summary_for_api_request_hook passes as usage=.
    plugin._post_api_request(
        turn_id="turn-u", model="openai/gpt-6-astra", response_model="gpt-6-astra-2026",
        usage={"input_tokens": 900, "cache_read_tokens": 100, "cache_write_tokens": 0,
               "output_tokens": 50, "reasoning_tokens": 10, "request_count": 1,
               "prompt_tokens": 1000, "total_tokens": 1050},
    )
    plugin._post_api_request(
        turn_id="turn-u", model="openai/gpt-6-astra", response_model="gpt-6-astra-2026",
        usage={"input_tokens": 1500, "output_tokens": 250, "prompt_tokens": 2000},
    )
    plugin._post_api_request(turn_id="other-turn", usage={"prompt_tokens": 7})

    record = outcome.reply_outcome_record(
        "agent:cleo", turn_id="turn-u", completed=True, model="openai/gpt-6-astra",
        platform="slack", now=NOW,
    )
    again = outcome.reply_outcome_record(
        "agent:cleo", turn_id="turn-u", completed=True, platform="slack", now=NOW,
    )

    assert record["inputTokens"] == 3000
    assert record["outputTokens"] == 300
    assert record["model"] == "gpt-6-astra-2026"
    assert record["costBasis"] == "unknown"
    assert "costUsd" not in record
    assert not set(outcome.USAGE_FIELDS) & set(again)


def test_a_turn_without_reported_usage_sends_no_usage_fields(plugin, outcome):
    plugin._post_api_request(turn_id="turn-n", usage=None, model="m")
    plugin._post_api_request(turn_id="turn-n", usage={"prompt_tokens": None}, model="m")

    record = outcome.reply_outcome_record(
        "agent:cleo", turn_id="turn-n", completed=True, model="m", platform="slack", now=NOW,
    )

    assert not set(outcome.USAGE_FIELDS) & set(record)


def test_a_rejection_of_the_usage_fields_retries_without_them(outcome, monkeypatch):
    calls = []

    def post(url, token, payload, *, session_id="", timeout=0):
        if payload["method"] == "initialize":
            return {"result": {}}, "sess-1", {}
        arguments = payload["params"]["arguments"]
        calls.append(arguments)
        if "inputTokens" in arguments:
            return {"error": {"code": -32602, "message": "Invalid arguments: unrecognized key 'inputTokens'"}}, session_id, {}
        return {"result": {"structuredContent": {"recorded": True}}}, session_id, {}

    monkeypatch.setattr(outcome, "_post", post)
    outcome.record_run("u", "t", {
        "agentRef": "agent:cleo", "idempotencyKey": "turn:t", "inputTokens": 10,
        "outputTokens": 2, "model": "m", "costBasis": "unknown",
    })

    assert calls == [
        {"agentRef": "agent:cleo", "idempotencyKey": "turn:t", "inputTokens": 10,
         "outputTokens": 2, "model": "m", "costBasis": "unknown"},
        {"agentRef": "agent:cleo", "idempotencyKey": "turn:t"},
    ]


def test_a_non_validation_refusal_with_usage_is_not_retried(outcome, monkeypatch):
    calls = []

    def post(url, token, payload, *, session_id="", timeout=0):
        if payload["method"] == "initialize":
            return {"result": {}}, "sess-1", {}
        calls.append(payload["params"]["arguments"])
        return {"result": {"isError": True, "content": "forbidden"}}, session_id, {}

    monkeypatch.setattr(outcome, "_post", post)
    with pytest.raises(RuntimeError):
        outcome.record_run("u", "t", {"agentRef": "a", "inputTokens": 1})
    assert len(calls) == 1


def test_plugin_reports_slack_turns_end_to_end(plugin, outcome, monkeypatch):
    reported = []
    monkeypatch.setenv("KATAILYST2_MCP_URL", "https://k2.example/mcp")
    monkeypatch.setenv("KATAILYST2_MCP_TOKEN", "token")
    monkeypatch.delenv("HLT_AGENT_REF", raising=False)
    monkeypatch.setenv("AGENT_ID", "cleo")
    monkeypatch.setattr(
        plugin, "report_in_background", lambda url, token, record: reported.append(record)
    )

    plugin._on_session_end(
        session_id="s", turn_id="turn-7", completed=True, failed=False,
        interrupted=False, turn_exit_reason="text_response(stop)",
        model="openai/gpt-6-astra", platform="slack", task_id="ignored",
    )
    plugin._on_session_end(turn_id="turn-8", completed=True, platform="cron")

    assert len(reported) == 1
    assert reported[0]["agentRef"] == "agent:cleo"
    assert reported[0]["idempotencyKey"] == "turn:turn-7"
