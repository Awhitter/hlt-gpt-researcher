"""Focused offline contracts for Cleo's bounded native scheduled checks."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "services" / "agent"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SERVICE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fleet = load("fleet_durability")
budget = load("fleet_run_budget")


def agent():
    return SimpleNamespace(
        max_iterations=4, max_tokens=1200, tools=[],
        session_output_tokens=0, session_completion_tokens=0,
        session_prompt_tokens=0, session_input_tokens=0,
        session_cache_read_tokens=0, session_cache_write_tokens=0,
    )


def test_budget_is_opt_in_and_uses_native_constructor_limits():
    assert budget.budget_kwargs({}, 24) == {"max_iterations": 24}
    kwargs = budget.budget_kwargs({"hlt_run_budget": budget.CANARY_BUDGET}, 24)
    assert kwargs == {"max_iterations": 4, "max_tokens": 1200, "run_budget_seconds": 120}
    assert budget.budget_kwargs({"hlt_run_budget": budget.CANARY_BUDGET}, 2)["max_iterations"] == 2


def test_non_native_runtime_cannot_bypass_the_budget():
    worker = agent()
    worker.api_mode = "codex_app_server"
    with pytest.raises(ValueError, match="native provider loop"):
        budget.attach_budget(worker, {"hlt_run_budget": budget.CANARY_BUDGET})


@pytest.mark.parametrize("value", [True, 0, -1, "1200", None])
def test_invalid_budget_cannot_silently_become_unbounded(value):
    limits = {**budget.CANARY_BUDGET, "max_output_tokens": value}
    with pytest.raises(ValueError):
        budget.budget_kwargs({"hlt_run_budget": limits}, 24)


def test_cumulative_output_reduces_next_request_and_blocks_grace_calls():
    worker = agent()
    budget.attach_budget(worker, {"hlt_run_budget": budget.CANARY_BUDGET})
    assert budget.admit_iteration(worker, [], 0)
    worker.session_output_tokens = 900
    assert budget.admit_iteration(worker, [], 1)
    assert worker.max_tokens == 300
    worker.session_output_tokens = 1200
    assert not budget.admit_iteration(worker, [], 2)
    worker.session_output_tokens = 0
    assert not budget.admit_iteration(worker, [], 4)


def test_elapsed_time_and_input_token_reservation_are_bounded(monkeypatch):
    worker = agent()
    budget.attach_budget(worker, {"hlt_run_budget": budget.CANARY_BUDGET})
    assert not budget.admit_request(worker, {"messages": [{"content": "x" * 64001}], "max_tokens": 1200})
    monkeypatch.setattr(budget.time, "monotonic", lambda: worker._hlt_scheduled_started + 121)
    assert not budget.admit_iteration(worker, [], 0)
    assert budget.admit_iteration(agent(), [{"content": "x" * 64001}], 200)


def test_input_reservation_reconciles_42kb_request_to_actual_12k_tokens():
    worker = agent()
    budget.attach_budget(worker, {"hlt_run_budget": budget.CANARY_BUDGET})
    request = {"input": "x" * 42000, "max_output_tokens": 1200}
    assert budget.admit_request(worker, request.copy())
    worker.session_prompt_tokens = 12000
    worker.session_input_tokens = 12000
    worker.session_output_tokens = 100
    assert budget.admit_request(worker, request.copy())
    assert worker._hlt_scheduled_observed_input == 12000
    assert worker._hlt_scheduled_reserved_input < 43000
    # A third full request cannot fit after another 12k tokens were consumed.
    worker.session_prompt_tokens = worker.session_input_tokens = 24000
    worker.session_output_tokens = 200
    assert not budget.admit_request(worker, request.copy())


def test_unknown_input_usage_retains_42kb_charge_despite_output_receipt():
    worker = agent()
    budget.attach_budget(worker, {"hlt_run_budget": budget.CANARY_BUDGET})
    request = {"input": "x" * 42000, "max_output_tokens": 1200}
    assert budget.admit_request(worker, request.copy())
    worker.session_output_tokens = 100
    assert not budget.admit_request(worker, request.copy())
    assert worker._hlt_scheduled_requests == 1


@pytest.mark.parametrize("inclusive_counter", [0, 12000])
def test_input_accounting_includes_cache_reads_and_writes_once(inclusive_counter):
    worker = agent()
    worker.session_prompt_tokens = inclusive_counter
    worker.session_input_tokens = 2000
    worker.session_cache_read_tokens = 9000
    worker.session_cache_write_tokens = 1000
    assert budget._observed_input_tokens(worker) == 12000
    budget.attach_budget(worker, {"hlt_run_budget": budget.CANARY_BUDGET})
    assert not budget.admit_request(worker, {"input": "x" * 55000, "max_output_tokens": 1200})


def test_later_input_receipt_does_not_erase_an_older_unknown_reservation():
    worker = agent()
    budget.attach_budget(worker, {"hlt_run_budget": budget.CANARY_BUDGET})
    request = {"input": "x" * 20000, "max_output_tokens": 1200}
    assert budget.admit_request(worker, request.copy())
    first_reserved = worker._hlt_scheduled_reserved_input
    # Output accounting exists, but this attempt's input usage is missing.
    worker.session_output_tokens = 100
    assert budget.admit_request(worker, request.copy())
    worker.session_prompt_tokens = worker.session_input_tokens = 5000
    worker.session_output_tokens = 200
    assert budget.admit_request(worker, request.copy())
    assert worker._hlt_scheduled_reserved_input == 2 * first_reserved


def test_input_token_exhaustion_blocks_iteration_even_with_output_remaining():
    worker = agent()
    budget.attach_budget(worker, {"hlt_run_budget": budget.CANARY_BUDGET})
    worker.session_prompt_tokens = 64000
    assert not budget.admit_iteration(worker, [], 1)


def test_actual_request_cap_overrides_retry_boost_and_reserves_unknown_spend():
    worker = agent()
    budget.attach_budget(worker, {"hlt_run_budget": budget.CANARY_BUDGET})
    request = {"max_output_tokens": 32768, "messages": []}
    assert budget.admit_request(worker, request)
    assert request["max_output_tokens"] == 1200
    # A timeout with no usage receipt must not trigger four paid retries.
    assert not budget.admit_request(worker, {"max_output_tokens": 32768})
    worker.session_output_tokens = 100
    request = {"max_output_tokens": 32768}
    assert budget.admit_request(worker, request)
    assert request["max_output_tokens"] == 1100


def test_budget_refuses_a_wire_without_an_output_cap():
    worker = agent()
    budget.attach_budget(worker, {"hlt_run_budget": budget.CANARY_BUDGET})
    assert not budget.admit_request(worker, {"model": "gpt-5.6-sol", "input": []})
    assert worker._hlt_scheduled_requests == 0


def health(ready=True, fallback=True):
    return {"readiness": {
        "ready": ready and fallback, "servingReady": ready,
        "redundancyReady": fallback,
        "checks": {"slack_socket_connected": ready, "fallback_model_profile_ready": fallback},
    }}


def test_unhealthy_is_a_completed_observation_and_alerts_only_on_change(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert fleet.check("readiness", lambda _: health()) == ""
    assert "fallback_model_profile_ready" in fleet.check("readiness", lambda _: health(fallback=False))
    assert fleet.check("readiness", lambda _: health(fallback=False)) == ""
    assert json.loads((tmp_path / "fleet/readiness.json").read_text())["servingReady"] is True
    assert "recovered" in fleet.check("readiness", lambda _: health())
    assert fleet.check("readiness", lambda _: health()) == ""


def test_failed_native_delivery_retries_same_observation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert fleet.check("readiness", lambda _: health(False))
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron/jobs.json").write_text(json.dumps({"jobs": [{
        "name": "hlt-fleet-readiness-cleo-v1", "last_delivery_error": "channel_not_found",
    }]}))
    assert fleet.check("readiness", lambda _: health(False))


def test_gate_allows_serving_even_with_degraded_redundancy_and_rejects_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fleet.check("readiness", lambda _: health(fallback=False))
    assert json.loads(fleet.gate())["wakeAgent"] is True
    path = tmp_path / "fleet/readiness.json"
    receipt = json.loads(path.read_text())
    receipt["checkedAt"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(receipt))
    assert json.loads(fleet.gate())["wakeAgent"] is False


def test_release_metadata_is_once_per_candidate_without_build_or_model(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_UPSTREAM_REF", "a" * 40)
    calls = []

    def fetch(url):
        calls.append(url)
        return {"sha": "b" * 40} if "/commits/" in url else {"tag_name": "v2026.9.2"}

    assert "No build or upgrade was triggered" in fleet.check("release", fetch)
    assert fleet.check("release", fetch) == ""
    assert len(calls) == 4
    assert all("api.github.com/repos/NousResearch/hermes-agent/" in url for url in calls)


def test_transport_failure_does_not_echo_raw_error_or_retry_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def broken(_):
        raise RuntimeError("secret-value from provider")

    message = fleet.check("readiness", broken)
    assert "RuntimeError" in message
    assert "secret-value" not in message
    assert fleet.check("readiness", broken) == ""


def test_installer_idempotently_uses_native_jobs_and_keeps_retired_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HLT_AGENT_REF", "agent:cleo")
    monkeypatch.setenv("AGENT_ID", "cleo")
    records = [{"id": "retired", "name": "nm-monday-brief", "enabled": False}]
    updates = []
    native = ModuleType("cron.jobs")
    native.list_jobs = lambda include_disabled: records
    native.parse_schedule = lambda value: {"kind": "cron", "expr": value}

    def create(**fields):
        row = {"id": str(len(records)), "enabled": True, **fields}
        row["schedule"] = native.parse_schedule(row["schedule"])
        records.append(row)
        return row

    def update(job_id, fields):
        updates.append((job_id, fields.copy()))
        row = next(row for row in records if row["id"] == job_id)
        row.update(fields)
        return row

    native.create_job, native.update_job = create, update
    monkeypatch.setitem(sys.modules, "cron.jobs", native)
    monkeypatch.setitem(sys.modules, "fleet_run_budget", budget)
    monkeypatch.setitem(sys.modules, "grounding", SimpleNamespace(grounding_dir=lambda home: home / "grounding"))
    installed = fleet.install()
    assert not installed["failed"]
    assert "ordinary Slack acceptance verifies Sol/high" in installed["canaryRoute"]["verifies"]
    assert not fleet.install()["failed"]
    assert len(records) == 4
    assert records[0] == {"id": "retired", "name": "nm-monday-brief", "enabled": False}
    canary = next(row for row in records if "daily-canary" in row["name"])
    assert canary["model"] == "grok-4.6" and canary["provider"] == "xai-oauth"
    assert canary["reasoning_effort"] == "high"
    assert canary["hlt_run_budget"] == budget.CANARY_BUDGET
    assert all(row["deliver"] == "slack:C0BH5997USK" for row in records[1:])
    assert sum(row["no_agent"] for row in records[1:]) == 2
    assert all("schedule" not in fields for _, fields in updates)
    assert len(list((tmp_path / "scripts").glob("hlt-fleet-*.py"))) == 3


def test_reusing_image_for_another_agent_does_not_seed_cleo_jobs(monkeypatch):
    monkeypatch.setenv("HLT_AGENT_REF", "agent:someone-else")
    assert fleet.install() == {"installed": [], "failed": [], "skipped": "different-agent"}
