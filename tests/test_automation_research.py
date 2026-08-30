import asyncio
import hashlib
import json
import sqlite3
from copy import deepcopy
from contextlib import suppress

import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from gpt_researcher.research_run_store import (
    INTERRUPTED_ERROR_CODE,
    ResearchRunStore,
)
import mcp_server.automation_research as automation
import mcp_server.automation_research_contracts as automation_contracts
from mcp_server.auth import BearerAuthMiddleware


def request_payload(*, request_id="make:research:001"):
    return {
        "schema_version": "research_automation_request.v1",
        "request_id": request_id,
        "query": "What do the required authorities document?",
        "report_prompt": "Cite every required source and state scope limits.",
        "report_type": "research_report",
        "report_source": "web",
        "tone": "Objective",
        "scope": "none",
        "depth": "balanced",
        "max_sources_per_query": 8,
        "include_generated_images": False,
        "source_policy": {
            "version": "source_policy.v1",
            "enforcement": "strict",
            "discovery_mode": "required_only",
            "allowed_domains": ["authority.example"],
            "denied_domains": [],
            "required_sources": [
                {
                    "id": "authority",
                    "family": "official",
                    "url": "https://authority.example/evidence",
                }
            ],
            "min_accepted_sources": 1,
            "min_content_chars": 100,
            "require_title": True,
            "require_required_sources_cited": True,
            "independent_judge_required": True,
        },
    }


def successful_deep_result():
    return {
        "status": "success",
        "source_count": 1,
        "source_urls": ["https://authority.example/evidence"],
        "source_manifest": {
            "version": "source_manifest.v1",
            "status": "passed",
            "accepted_sources": [
                {"canonical_url": "https://authority.example/evidence"}
            ],
        },
        "image_count": 0,
        "images": [],
    }


def successful_report_result():
    return {
        "status": "success",
        "publishable": True,
        "report": "# Accepted report\n\nSupported claim [source](https://authority.example/evidence).",
        "report_path": "/data/outputs/accepted.md",
        "source_count": 1,
        "image_count": 0,
        "images": [],
        "costs": 0.125,
        "source_manifest": successful_deep_result()["source_manifest"],
        "report_quality": {
            "version": "report_quality.v1",
            "status": "passed",
            "publishable": True,
            "missing_required_citations": [],
            "unadmitted_citations": [],
            "independent_judgment": {
                "verdict": "pass",
                "findings": [],
                "claim_checks": [
                    {
                        "claim": "The authority documents the supported claim.",
                        "supported": True,
                        "source_urls": ["https://authority.example/evidence"],
                    }
                ],
            },
        },
    }


@pytest.fixture(autouse=True)
def clear_adapter_state():
    automation.clear_automation_hot_state()
    yield
    automation.clear_automation_hot_state()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.pop("schema_version"),
        lambda body: body.update(schema_version="research_automation_request.v2"),
        lambda body: body["source_policy"].update(enforcement="advisory"),
        lambda body: body.update(max_sources_per_query=2),
        lambda body: body.update(unexpected=True),
        lambda body: body["source_policy"].update(unexpected=True),
        lambda body: body["source_policy"]["required_sources"][0].update(
            unexpected=True
        ),
        lambda body: body.update(request_id="contains spaces"),
        lambda body: body.update(query="   "),
        lambda body: body.update(scope="auto"),
        lambda body: body.update(scope=["codebase"]),
    ],
)
def test_request_validation_is_versioned_strict_and_forbids_extras(mutate):
    payload = request_payload()
    mutate(payload)

    with pytest.raises(ValidationError):
        automation.AutomationResearchRequest.model_validate(payload)


def test_fingerprint_is_canonical_and_research_identity_is_deterministic():
    first_payload = request_payload(request_id="caller-one")
    second_payload = request_payload(request_id="caller-two")
    first_payload.pop("scope")
    second_payload["scope"] = "none"
    second_payload["source_policy"]["allowed_domains"] = [
        "authority.example",
        "AUTHORITY.EXAMPLE",
    ]
    second_payload["source_policy"]["required_sources"][0]["url"] = (
        "https://AUTHORITY.example/evidence/?utm_source=make"
    )

    first = automation.AutomationResearchRequest.model_validate(first_payload)
    second = automation.AutomationResearchRequest.model_validate(second_payload)
    first_fingerprint = automation.request_fingerprint(first)
    second_fingerprint = automation.request_fingerprint(second)

    assert first_fingerprint == second_fingerprint
    assert len(first_fingerprint) == 64
    assert automation.deterministic_research_id(
        first.request_id, first_fingerprint
    ) == automation.deterministic_research_id(first.request_id, first_fingerprint)
    assert automation.deterministic_research_id(
        first.request_id, first_fingerprint
    ) != automation.deterministic_research_id(second.request_id, second_fingerprint)


def test_store_migrates_running_receipts_to_fenced_leases(tmp_path):
    path = tmp_path / "runs.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE automation_research_requests (
                request_id TEXT PRIMARY KEY,
                request_fingerprint TEXT NOT NULL,
                research_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                response_json TEXT,
                error_code TEXT,
                error_message TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO automation_research_requests (
                request_id, request_fingerprint, research_id, status,
                started_at, updated_at
            ) VALUES (?, ?, ?, 'running', ?, ?)
            """,
            ("legacy-request", "f" * 64, "legacy-research", "start", "update"),
        )

    store = automation.AutomationResearchStore(path)
    migrated = store.get("legacy-request")

    assert migrated["lease_token"]
    assert migrated["lease_generation"] == 1
    assert migrated["request"] is None
    assert (
        migrated["receipt_schema_version"]
        == automation.LEGACY_DURABLE_RECEIPT_SCHEMA_VERSION
    )


def test_store_reads_implicit_deadlines_from_the_contract_owner(
    monkeypatch, tmp_path
):
    deadlines = automation.AutomationDeadlines(7, 6, 5)
    monkeypatch.setattr(
        automation_contracts, "_automation_deadlines", lambda: deadlines
    )
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    fingerprint = automation.request_fingerprint(request)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")

    row, _ = store.reserve(
        request.request_id,
        fingerprint,
        automation.deterministic_research_id(request.request_id, fingerprint),
        request_payload=request.model_dump(mode="json", exclude_none=True),
    )

    assert row["overall_timeout_seconds"] == 7
    assert row["deep_timeout_seconds"] == 6
    assert row["report_timeout_seconds"] == 5


def test_first_run_calls_strict_engine_and_persists_accepted_result(
    monkeypatch, tmp_path
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    calls = []

    async def fake_deep(query, report_type, report_source, tone, **kwargs):
        calls.append(("deep", query, report_type, report_source, tone, kwargs))
        return successful_deep_result()

    async def fake_report(research_id, prompt):
        calls.append(("report", research_id, prompt))
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)

    result = asyncio.run(automation.execute_automation_research(request, store=store))

    assert result.status_code == 200
    assert result.body["status"] == "completed"
    assert result.body["publishable"] is True
    assert result.body["idempotent_readback"] is False
    assert result.body["report"].startswith("# Accepted report")
    assert result.body["source_manifest"]["status"] == "passed"
    assert result.body["report_quality"]["status"] == "passed"
    assert result.body["cost_usd"] == 0.125
    assert result.body["delivery"] == {"attempted": False}
    expected_id = automation.deterministic_research_id(
        request.request_id, automation.request_fingerprint(request)
    )
    assert result.body["research_id"] == expected_id
    assert result.body["core_run_id"] != expected_id
    assert calls[0][5]["_research_id"] == result.body["core_run_id"]
    assert calls[0][5]["source_policy"]["enforcement"] == "strict"
    assert calls[0][5]["scope"] == "none"
    assert calls[1] == ("report", result.body["core_run_id"], request.report_prompt)
    receipt = store.get(request.request_id)
    assert receipt["response"]["status"] == "completed"
    assert receipt["core_run_id"] == automation.attempt_research_id(
        receipt["research_id"], receipt["lease_generation"], receipt["lease_token"]
    )


def test_same_payload_is_a_durable_readback_without_second_spend(monkeypatch, tmp_path):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    path = tmp_path / "runs.sqlite3"
    first_store = automation.AutomationResearchStore(path)
    calls = {"deep": 0, "report": 0}

    async def fake_deep(*_args, **_kwargs):
        calls["deep"] += 1
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        calls["report"] += 1
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)
    first = asyncio.run(
        automation.execute_automation_research(request, store=first_store)
    )

    automation.clear_automation_hot_state()
    reopened = automation.AutomationResearchStore(path)

    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("durable idempotent readback must not spend again")

    monkeypatch.setattr(automation, "deep_research_tool", should_not_run)
    monkeypatch.setattr(automation, "write_report_tool", should_not_run)
    replay = asyncio.run(
        automation.execute_automation_research(request, store=reopened)
    )

    assert first.body["idempotent_readback"] is False
    assert replay.status_code == 200
    assert replay.body["idempotent_readback"] is True
    assert replay.body["research_id"] == first.body["research_id"]
    assert replay.body["report"] == first.body["report"]
    assert calls == {"deep": 1, "report": 1}


def test_changed_payload_under_same_request_id_is_409_without_work(
    monkeypatch, tmp_path
):
    first_request = automation.AutomationResearchRequest.model_validate(
        request_payload()
    )
    changed_payload = deepcopy(request_payload())
    changed_payload["query"] = "A different semantic question"
    changed_request = automation.AutomationResearchRequest.model_validate(
        changed_payload
    )
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    calls = {"deep": 0}

    async def fake_deep(*_args, **_kwargs):
        calls["deep"] += 1
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)
    first = asyncio.run(
        automation.execute_automation_research(first_request, store=store)
    )
    conflict = asyncio.run(
        automation.execute_automation_research(changed_request, store=store)
    )

    assert first.body["status"] == "completed"
    assert conflict.status_code == 409
    assert conflict.body["error_code"] == "idempotency_conflict"
    assert conflict.body["research_id"] == first.body["research_id"]
    assert calls["deep"] == 1


def test_report_quality_failure_is_fail_closed_and_durable(monkeypatch, tmp_path):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    calls = {"report": 0}

    async def fake_deep(*_args, **_kwargs):
        return successful_deep_result()

    async def rejected_report(*_args, **_kwargs):
        calls["report"] += 1
        return {
            "status": "error",
            "error_code": "report_quality_failed",
            "message": "Independent source acceptance did not pass.",
            "publishable": False,
            "draft_report": "Unsupported draft must not be returned as report.",
            "report_path": "/data/outputs/rejected.md",
            "source_manifest": successful_deep_result()["source_manifest"],
            "report_quality": {
                "version": "report_quality.v1",
                "status": "failed",
                "publishable": False,
            },
            "costs": 0.05,
        }

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", rejected_report)
    failed = asyncio.run(automation.execute_automation_research(request, store=store))
    replay = asyncio.run(automation.execute_automation_research(request, store=store))

    assert failed.status_code == 200
    assert failed.body["status"] == "failed"
    assert failed.body["publishable"] is False
    assert failed.body["error_code"] == "report_quality_failed"
    assert failed.body["report"] is None
    assert failed.body["report_quality"]["status"] == "failed"
    assert failed.body["delivery"] == {"attempted": False}
    assert replay.body["idempotent_readback"] is True
    assert replay.body["report"] is None
    assert calls["report"] == 1


@pytest.mark.parametrize(
    "quality_override",
    [
        {"missing_required_citations": ["https://authority.example/evidence"]},
        {"unadmitted_citations": ["https://unapproved.example/source"]},
        {"independent_judgment": {"verdict": "repair_required"}},
        {
            "independent_judgment": {
                "verdict": "pass",
                "findings": [],
                "claim_checks": [],
            }
        },
        {
            "independent_judgment": {
                "verdict": "pass",
                "findings": [],
                "claim_checks": [
                    {
                        "claim": "Unsupported claim",
                        "supported": False,
                        "source_urls": ["https://authority.example/evidence"],
                    }
                ],
            }
        },
        {
            "independent_judgment": {
                "verdict": "pass",
                "findings": [],
                "claim_checks": [
                    {
                        "claim": "   ",
                        "supported": True,
                        "source_urls": ["https://authority.example/evidence"],
                    }
                ],
            }
        },
        {
            "independent_judgment": {
                "verdict": "pass",
                "findings": [],
                "claim_checks": [
                    {
                        "claim": "Malformed source receipt",
                        "supported": True,
                        "source_urls": [123],
                    }
                ],
            }
        },
    ],
)
def test_success_label_cannot_bypass_explicit_acceptance_invariants(
    monkeypatch, tmp_path, quality_override
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    report = successful_report_result()
    report["report_quality"].update(quality_override)

    async def fake_deep(*_args, **_kwargs):
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        return report

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)

    result = asyncio.run(automation.execute_automation_research(request, store=store))

    assert result.status_code == 200
    assert result.body["status"] == "failed"
    assert result.body["publishable"] is False
    assert result.body["error_code"] == "acceptance_invariant_failed"
    assert result.body["report"] is None


@pytest.mark.parametrize("report", [None, "", "   "])
def test_missing_or_empty_report_fails_closed(monkeypatch, tmp_path, report):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    report_result = successful_report_result()
    report_result["report"] = report

    async def fake_deep(*_args, **_kwargs):
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        return report_result

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)

    result = asyncio.run(automation.execute_automation_research(request, store=store))

    assert result.status_code == 200
    assert result.body["status"] == "failed"
    assert result.body["publishable"] is False
    assert result.body["error_code"] == "acceptance_invariant_failed"
    assert result.body["report"] is None


@pytest.mark.parametrize(
    ("report_publishable", "quality_publishable"),
    [(False, True), (True, False), (None, True)],
)
def test_publishable_flags_must_independently_pass(
    monkeypatch, tmp_path, report_publishable, quality_publishable
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    report_result = successful_report_result()
    report_result["publishable"] = report_publishable
    report_result["report_quality"]["publishable"] = quality_publishable

    async def fake_deep(*_args, **_kwargs):
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        return report_result

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)

    result = asyncio.run(automation.execute_automation_research(request, store=store))

    assert result.status_code == 200
    assert result.body["status"] == "failed"
    assert result.body["publishable"] is False
    assert result.body["error_code"] == "acceptance_invariant_failed"
    assert result.body["report"] is None


def test_concurrent_duplicate_gets_immediate_202_without_duplicate_work(
    monkeypatch, tmp_path
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    calls = {"deep": 0, "report": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_deep(*_args, **_kwargs):
        calls["deep"] += 1
        started.set()
        await release.wait()
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        calls["report"] += 1
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)

    async def scenario():
        owner_task = asyncio.create_task(
            automation.execute_automation_research(request, store=store)
        )
        await started.wait()
        retry = await asyncio.wait_for(
            automation.execute_automation_research(request, store=store),
            timeout=0.25,
        )
        release.set()
        owner = await owner_task
        replay = await automation.execute_automation_research(request, store=store)
        return owner, retry, replay

    owner, retry, replay = asyncio.run(scenario())

    assert owner.status_code == 200
    assert owner.body["status"] == "completed"
    assert retry.status_code == 202
    assert retry.headers == {"Retry-After": "5"}
    assert retry.body["status"] == "running"
    assert replay.status_code == 200
    assert replay.body["idempotent_readback"] is True
    assert calls == {"deep": 1, "report": 1}
    assert automation._request_locks == {}


def test_existing_running_receipt_returns_202_without_duplicate_work(
    monkeypatch, tmp_path
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    fingerprint = automation.request_fingerprint(request)
    research_id = automation.deterministic_research_id(request.request_id, fingerprint)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    store.reserve(request.request_id, fingerprint, research_id)

    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("an existing running request must not start twice")

    monkeypatch.setattr(automation, "deep_research_tool", should_not_run)
    result = asyncio.run(automation.execute_automation_research(request, store=store))

    assert result.status_code == 202
    assert result.headers == {"Retry-After": "5"}
    assert result.body["status"] == "running"
    assert result.body["research_id"] == research_id
    assert automation._request_locks == {}


def test_stale_deep_complete_receipt_resumes_report_without_second_research(
    monkeypatch, tmp_path
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    fingerprint = automation.request_fingerprint(request)
    research_id = automation.deterministic_research_id(request.request_id, fingerprint)
    path = tmp_path / "runs.sqlite3"
    store = automation.AutomationResearchStore(path)
    initial, _ = store.reserve(request.request_id, fingerprint, research_id)
    initial_core_id = initial["core_run_id"]
    core = ResearchRunStore(path, recover_interrupted=False)
    core.create_run(
        initial_core_id,
        query=request.query,
        report_type=request.report_type,
        report_source=request.report_source,
        tone=request.tone,
        source_policy=request.source_policy.model_dump(exclude_none=True),
    )
    core.complete_run(
        initial_core_id,
        context="accepted source context",
        sources=[
            {
                "title": "Authority",
                "url": "https://authority.example/evidence",
                "content": "Evidence " * 20,
            }
        ],
        source_urls=["https://authority.example/evidence"],
        costs=0.08,
        source_policy=request.source_policy.model_dump(exclude_none=True),
        source_manifest=successful_deep_result()["source_manifest"],
    )
    monkeypatch.setattr(automation, "_stale_cutoff_iso", lambda: "9999-01-01")

    async def should_not_research(*_args, **_kwargs):
        raise AssertionError("stale deep-complete run must resume at report writing")

    async def fake_report(received_id, prompt):
        assert received_id != research_id
        assert prompt == request.report_prompt
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", should_not_research)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)

    result = asyncio.run(automation.execute_automation_research(request, store=store))

    assert result.status_code == 200
    assert result.body["status"] == "completed"
    assert result.body["research_id"] == research_id
    assert result.body["core_run_id"] != research_id
    cloned = core.get_run(result.body["core_run_id"])
    assert cloned["context"] == "accepted source context"
    assert cloned["source_manifest"]["status"] == "passed"
    assert store.get(request.request_id)["status"] == "completed"
    assert automation._request_locks == {}


def test_interrupted_restart_waits_for_stale_lease_then_reruns(monkeypatch, tmp_path):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    fingerprint = automation.request_fingerprint(request)
    research_id = automation.deterministic_research_id(request.request_id, fingerprint)
    path = tmp_path / "runs.sqlite3"
    store = automation.AutomationResearchStore(path)
    initial, created = store.reserve(request.request_id, fingerprint, research_id)
    assert created is True

    core = ResearchRunStore(path, recover_interrupted=False)
    core.create_run(
        initial["core_run_id"],
        query=request.query,
        report_type=request.report_type,
        report_source=request.report_source,
        tone=request.tone,
        source_policy=request.source_policy.model_dump(exclude_none=True),
    )
    restarted = ResearchRunStore(path, recover_interrupted=True)
    interrupted = restarted.get_run(initial["core_run_id"])
    assert interrupted["status"] == "failed"
    assert interrupted["error_code"] == INTERRUPTED_ERROR_CODE

    calls = {"deep": 0, "report": 0}

    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("a fresh adapter lease must remain recoverable as 202")

    monkeypatch.setattr(automation, "deep_research_tool", should_not_run)
    waiting = asyncio.run(automation.execute_automation_research(request, store=store))

    assert waiting.status_code == 202
    assert waiting.body["status"] == "running"
    assert store.get(request.request_id)["lease_token"] == initial["lease_token"]
    assert store.get(request.request_id)["lease_generation"] == 1

    monkeypatch.setattr(automation, "_stale_cutoff_iso", lambda: "9999-01-01")

    async def fake_deep(*_args, **_kwargs):
        calls["deep"] += 1
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        calls["report"] += 1
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)
    recovered = asyncio.run(
        automation.execute_automation_research(request, store=store)
    )

    receipt = store.get(request.request_id)
    assert recovered.status_code == 200
    assert recovered.body["status"] == "completed"
    assert recovered.body["error_code"] is None
    assert receipt["status"] == "completed"
    assert receipt["lease_generation"] == 2
    assert receipt["lease_token"] != initial["lease_token"]
    assert calls == {"deep": 1, "report": 1}


def test_stale_owner_is_fenced_after_reclaimer_finishes(monkeypatch, tmp_path):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = {"deep": 0, "report": 0}

    async def fake_deep(*_args, **_kwargs):
        calls["deep"] += 1
        if calls["deep"] == 1:
            first_started.set()
            await release_first.wait()
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        calls["report"] += 1
        result = successful_report_result()
        result["report"] = (
            "# Reclaimer result\n\nSupported claim "
            "[source](https://authority.example/evidence)."
            if calls["report"] == 1
            else "# Stale owner result\n\nMust never replace the fenced receipt."
        )
        return result

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)
    monkeypatch.setattr(automation, "_stale_cutoff_iso", lambda: "9999-01-01")

    async def scenario():
        original_task = asyncio.create_task(
            automation.execute_automation_research(request, store=store)
        )
        await first_started.wait()
        original_lease = store.get(request.request_id)
        reclaimed = await automation.execute_automation_research(request, store=store)
        receipt_after_reclaim = store.get(request.request_id)
        release_first.set()
        original = await original_task
        return original_lease, reclaimed, receipt_after_reclaim, original

    original_lease, reclaimed, receipt_after_reclaim, original = asyncio.run(scenario())

    assert reclaimed.status_code == 200
    assert reclaimed.body["report"].startswith("# Reclaimer result")
    assert receipt_after_reclaim["lease_generation"] == 2
    assert receipt_after_reclaim["lease_token"] != original_lease["lease_token"]
    assert original.status_code == 200
    assert original.body["idempotent_readback"] is True
    assert original.body["report"] == reclaimed.body["report"]
    assert (
        store.get(request.request_id)["response"]["report"] == reclaimed.body["report"]
    )
    # The stale owner can finish its already-started research call, but the
    # ownership checkpoint fences it before a second report artifact/spend.
    assert calls == {"deep": 2, "report": 1}
    assert automation._request_locks == {}


def test_active_owner_heartbeats_without_rotating_lease(monkeypatch, tmp_path):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    heartbeat_calls = []
    real_heartbeat = store.heartbeat

    def recording_heartbeat(request_id, fingerprint, lease_token):
        heartbeat_calls.append((request_id, fingerprint, lease_token))
        return real_heartbeat(request_id, fingerprint, lease_token)

    async def slow_deep(*_args, **_kwargs):
        await asyncio.sleep(0.04)
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        return successful_report_result()

    monkeypatch.setattr(automation, "_heartbeat_interval_seconds", lambda: 0.005)
    monkeypatch.setattr(store, "heartbeat", recording_heartbeat)
    monkeypatch.setattr(automation, "deep_research_tool", slow_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)

    result = asyncio.run(automation.execute_automation_research(request, store=store))

    receipt = store.get(request.request_id)
    assert result.status_code == 200
    assert result.body["status"] == "completed"
    assert heartbeat_calls
    assert all(call[2] == receipt["lease_token"] for call in heartbeat_calls)
    assert receipt["lease_generation"] == 1


def test_runtime_failure_is_terminal_durable_and_cleans_lock(monkeypatch, tmp_path):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")

    async def explode(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(automation, "deep_research_tool", explode)
    first = asyncio.run(automation.execute_automation_research(request, store=store))
    replay = asyncio.run(automation.execute_automation_research(request, store=store))

    assert first.status_code == 200
    assert first.body["status"] == "failed"
    assert first.body["error_code"] == "adapter_runtime_error"
    assert first.body["error_message"] == "RuntimeError"
    assert replay.body["idempotent_readback"] is True
    assert automation._request_locks == {}


def test_route_returns_json_safe_422_for_nested_validation_errors():
    payload = request_payload()
    payload["source_policy"]["required_sources"][0]["unexpected"] = True
    app = Starlette(
        routes=[
            Route(
                "/automation/research/v1",
                automation.automation_research_route,
                methods=["POST"],
            )
        ]
    )
    response = TestClient(app).post("/automation/research/v1", json=payload)

    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_request"
    assert response.json()["delivery"] == {"attempted": False}


def test_bearer_middleware_remains_the_route_authority(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "test-token")

    async def endpoint(_request):
        return JSONResponse({"status": "reached"})

    app = Starlette(
        routes=[
            Route("/automation/research/v1", endpoint, methods=["POST"]),
            Route(
                "/automation/research/jobs/v1/start", endpoint, methods=["POST"]
            ),
            Route(
                "/automation/research/jobs/v1/{request_id}/status",
                endpoint,
                methods=["GET"],
            ),
            Route(
                "/automation/research/jobs/v1/{request_id}/result",
                endpoint,
                methods=["GET"],
            ),
        ]
    )
    app.add_middleware(BearerAuthMiddleware)
    client = TestClient(app)

    requests = [
        ("post", "/automation/research/v1"),
        ("post", "/automation/research/jobs/v1/start"),
        ("get", "/automation/research/jobs/v1/auth-test/status"),
        ("get", "/automation/research/jobs/v1/auth-test/result"),
    ]
    for method, path in requests:
        call = getattr(client, method)
        assert call(path).status_code == 401
        assert call(
            path, headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        allowed = call(
            path, headers={"Authorization": "Bearer test-token"}
        )
        assert allowed.status_code == 200
        assert allowed.json() == {"status": "reached"}


def test_async_start_is_immediate_idempotent_and_conflict_safe(monkeypatch, tmp_path):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    changed_payload = deepcopy(request_payload())
    changed_payload["query"] = "A conflicting request under the same idempotency key"
    changed_request = automation.AutomationResearchRequest.model_validate(changed_payload)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"deep": 0, "report": 0}

    async def slow_deep(*_args, **_kwargs):
        calls["deep"] += 1
        started.set()
        await release.wait()
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        calls["report"] += 1
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", slow_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)

    async def scenario():
        first = await asyncio.wait_for(
            automation.start_automation_research(request, store=store),
            timeout=0.5,
        )
        await started.wait()
        task = automation._background_tasks[
            automation._background_task_key(store, request.request_id)
        ]
        replay = await asyncio.wait_for(
            automation.start_automation_research(request, store=store),
            timeout=0.5,
        )
        conflict = await automation.start_automation_research(
            changed_request, store=store
        )
        release.set()
        await task
        terminal_replay = await automation.start_automation_research(
            request, store=store
        )
        return first, replay, conflict, terminal_replay

    first, replay, conflict, terminal_replay = asyncio.run(scenario())

    assert first.status_code == 202
    assert first.body["schema_version"] == "research_automation_operation.v1"
    assert first.body["operation"] == "mastery_research_start"
    assert first.body["status"] == "running"
    assert first.body["idempotent_readback"] is False
    assert first.body["status_url"].endswith("/make%3Aresearch%3A001/status")
    assert first.body["result_url"].endswith("/make%3Aresearch%3A001/result")
    assert replay.status_code == 202
    assert replay.body["idempotent_readback"] is True
    assert replay.body["research_id"] == first.body["research_id"]
    assert conflict.status_code == 409
    assert conflict.body["error_code"] == "idempotency_conflict"
    assert terminal_replay.status_code == 200
    assert terminal_replay.body["status"] == "completed"
    assert terminal_replay.body["result_ready"] is True
    assert terminal_replay.body["idempotent_readback"] is True
    assert calls == {"deep": 1, "report": 1}
    assert store.get(request.request_id)["request"]["query"] == request.query


def test_async_status_and_pending_result_are_pure_reads(monkeypatch, tmp_path):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    fingerprint = automation.request_fingerprint(request)
    research_id = automation.deterministic_research_id(request.request_id, fingerprint)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    before, created = store.reserve(
        request.request_id,
        fingerprint,
        research_id,
        request_payload=request.model_dump(mode="json", exclude_none=True),
    )
    assert created is True

    def mutation_is_forbidden(*_args, **_kwargs):
        raise AssertionError("status/result reads must not claim, heartbeat, or finish")

    monkeypatch.setattr(store, "claim_stale", mutation_is_forbidden)
    monkeypatch.setattr(store, "claim_for_reconciliation", mutation_is_forbidden)
    monkeypatch.setattr(store, "heartbeat", mutation_is_forbidden)
    monkeypatch.setattr(store, "finish", mutation_is_forbidden)

    status = automation.read_automation_research_status(
        request.request_id, store=store
    )
    result = automation.read_automation_research_result(
        request.request_id, store=store
    )
    missing_status = automation.read_automation_research_status(
        "missing-request", store=store
    )
    missing_result = automation.read_automation_research_result(
        "missing-request", store=store
    )
    after = store.get(request.request_id)

    assert status.status_code == 200
    assert status.body["operation"] == "mastery_research_status"
    assert status.body["status"] == "running"
    assert status.body["result_ready"] is False
    assert status.headers == {"Retry-After": "5"}
    assert result.status_code == 202
    assert result.headers == {"Retry-After": "5"}
    assert result.body["operation"] == "mastery_research_result"
    assert result.body["status"] == "running"
    assert missing_status.status_code == 404
    assert missing_result.status_code == 404
    assert after["lease_token"] == before["lease_token"]
    assert after["lease_generation"] == before["lease_generation"]
    assert after["updated_at"] == before["updated_at"]


def test_cold_status_and_result_reads_do_not_create_a_store(monkeypatch, tmp_path):
    path = tmp_path / "missing" / "runs.sqlite3"
    monkeypatch.setenv("RESEARCH_RUN_STORE_PATH", str(path))
    automation.clear_automation_hot_state()

    status = automation.read_automation_research_status("not-created")
    result = automation.read_automation_research_result("not-created")

    assert status.status_code == 503
    assert status.body["schema_version"] == "research_automation_status.v1"
    assert status.body["error_code"] == "automation_store_unavailable"
    assert result.status_code == 503
    assert result.body["schema_version"] == "research_automation_result_read.v1"
    assert result.body["error_code"] == "automation_store_unavailable"
    assert not path.exists()
    assert not path.parent.exists()


def test_cold_read_only_connection_observes_existing_wal_receipt(monkeypatch, tmp_path):
    path = tmp_path / "runs.sqlite3"
    monkeypatch.setenv("RESEARCH_RUN_STORE_PATH", str(path))
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    fingerprint = automation.request_fingerprint(request)
    research_id = automation.deterministic_research_id(request.request_id, fingerprint)
    store = automation.AutomationResearchStore(path)
    before, created = store.reserve(
        request.request_id,
        fingerprint,
        research_id,
        request_payload=request.model_dump(mode="json", exclude_none=True),
    )
    assert created is True
    automation.clear_automation_hot_state()

    status = automation.read_automation_research_status(request.request_id)
    result = automation.read_automation_research_result(request.request_id)
    after = store.get(request.request_id)

    assert status.status_code == 200
    assert status.body["status"] == "running"
    assert result.status_code == 202
    assert after["lease_token"] == before["lease_token"]
    assert after["lease_generation"] == before["lease_generation"]
    assert after["updated_at"] == before["updated_at"]


def test_async_read_routes_accept_the_encoded_urls_returned_by_start(monkeypatch):
    seen = []

    def fake_status(request_id):
        seen.append(("status", request_id))
        return automation.AutomationHTTPResult(200, {"status": "running"})

    def fake_result(request_id):
        seen.append(("result", request_id))
        return automation.AutomationHTTPResult(202, {"status": "running"})

    monkeypatch.setattr(automation, "read_automation_research_status", fake_status)
    monkeypatch.setattr(automation, "read_automation_research_result", fake_result)
    app = Starlette(
        routes=[
            Route(
                "/automation/research/jobs/v1/{request_id}/status",
                automation.automation_research_status_route,
                methods=["GET"],
            ),
            Route(
                "/automation/research/jobs/v1/{request_id}/result",
                automation.automation_research_result_route,
                methods=["GET"],
            ),
        ]
    )
    client = TestClient(app)

    assert client.get(
        "/automation/research/jobs/v1/make%3Aresearch%3A001/status"
    ).status_code == 200
    assert client.get(
        "/automation/research/jobs/v1/make%3Aresearch%3A001/result"
    ).status_code == 202
    assert seen == [
        ("status", "make:research:001"),
        ("result", "make:research:001"),
    ]


def test_async_terminal_result_has_evidence_provenance_and_k2_handoff(
    monkeypatch, tmp_path
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")

    async def fake_deep(*_args, **_kwargs):
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)

    async def scenario():
        started = await automation.start_automation_research(request, store=store)
        task = automation._background_tasks[
            automation._background_task_key(store, request.request_id)
        ]
        await task
        return started

    started = asyncio.run(scenario())
    status = automation.read_automation_research_status(request.request_id, store=store)
    result = automation.read_automation_research_result(request.request_id, store=store)

    assert started.status_code == 202
    assert status.body["status"] == "completed"
    assert status.body["result_ready"] is True
    assert result.status_code == 200
    assert result.body["schema_version"] == "research_automation_result_read.v1"
    assert result.body["operation"] == "mastery_research_result"
    assert result.body["report"].startswith("# Accepted report")
    assert result.body["evidence"] == {
        "source_count": 1,
        "source_urls": ["https://authority.example/evidence"],
        "source_manifest": successful_deep_result()["source_manifest"],
        "image_count": 0,
        "images": [],
    }
    assert result.body["quality"]["status"] == "passed"
    assert result.body["cost"] == {"amount": 0.125, "currency": "USD"}
    provenance = result.body["provenance"]
    assert provenance["schema_version"] == "research_automation_provenance.v1"
    assert provenance["provider"] == "mastery-research"
    assert provenance["capability_ref"] == "tool:mastery-research"
    assert provenance["request_fingerprint"].startswith("sha256:")
    assert provenance["source_policy"]["enforcement"] == "strict"
    handoff = result.body["k2_handoff"]
    assert handoff["schema_version"] == "k2_knowledge_refine_preview_handoff.v1"
    assert handoff["tool"] == "knowledge.refine.preview"
    arguments = handoff["arguments"]
    assert arguments["content"] == result.body["report"]
    assert arguments["transport"] == "automation"
    assert arguments["provenanceType"] == "derived_synthesis"
    assert arguments["sourceLinks"] == ["https://authority.example/evidence"]
    upstream = arguments["upstreamExecution"]
    assert upstream == {
        "schemaVersion": "upstream_execution_receipt.v1",
        "provider": "mastery-research",
        "capabilityRef": "tool:mastery-research",
        "operation": "mastery_research_result",
        "operationTitle": "Mastery Research result",
        "contractFingerprint": automation.ASYNC_CONTRACT_FINGERPRINT,
        "requestFingerprint": provenance["request_fingerprint"],
        "sourceSnapshotId": result.body["source_snapshot_id"],
        "auditId": f"mastery-research:{request.request_id}",
        "runId": result.body["core_run_id"],
        "status": "succeeded",
        "startedAt": result.body["started_at"],
        "completedAt": result.body["completed_at"],
    }


def test_async_failed_result_keeps_receipts_but_withholds_k2_content(
    monkeypatch, tmp_path
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")

    async def fake_deep(*_args, **_kwargs):
        return successful_deep_result()

    async def rejected_report(*_args, **_kwargs):
        result = successful_report_result()
        result.update(
            status="error",
            publishable=False,
            report=None,
            error_code="report_quality_failed",
            message="Independent source acceptance did not pass.",
        )
        result["report_quality"] = {
            "version": "report_quality.v1",
            "status": "failed",
            "publishable": False,
        }
        return result

    monkeypatch.setattr(automation, "deep_research_tool", fake_deep)
    monkeypatch.setattr(automation, "write_report_tool", rejected_report)

    async def scenario():
        await automation.start_automation_research(request, store=store)
        task = automation._background_tasks[
            automation._background_task_key(store, request.request_id)
        ]
        await task

    asyncio.run(scenario())
    result = automation.read_automation_research_result(request.request_id, store=store)

    assert result.status_code == 200
    assert result.body["status"] == "failed"
    assert result.body["report"] is None
    assert result.body["quality"]["status"] == "failed"
    assert result.body["provenance"]["source_policy"]["enforcement"] == "strict"
    assert result.body["upstream_execution"]["status"] == "failed"
    assert result.body["k2_handoff"] is None


def test_async_restart_replay_reclaims_stale_work_without_changing_identity(
    monkeypatch, tmp_path
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    path = tmp_path / "runs.sqlite3"
    store = automation.AutomationResearchStore(path)
    first_started = asyncio.Event()
    never_release = asyncio.Event()

    async def interrupted_deep(*_args, **_kwargs):
        first_started.set()
        await never_release.wait()
        raise AssertionError("cancelled background work must not continue")

    monkeypatch.setattr(automation, "deep_research_tool", interrupted_deep)

    async def interrupt_first_worker():
        first = await automation.start_automation_research(request, store=store)
        await first_started.wait()
        task = automation._background_tasks[
            automation._background_task_key(store, request.request_id)
        ]
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return first

    first = asyncio.run(interrupt_first_worker())
    interrupted_receipt = store.get(request.request_id)
    assert interrupted_receipt["status"] == "running"
    assert interrupted_receipt["response"] is None

    automation.clear_automation_hot_state()
    reopened = automation.AutomationResearchStore(path)
    monkeypatch.setattr(automation, "_stale_cutoff_iso", lambda: "9999-01-01")

    async def recovered_deep(*_args, **_kwargs):
        return successful_deep_result()

    async def recovered_report(*_args, **_kwargs):
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", recovered_deep)
    monkeypatch.setattr(automation, "write_report_tool", recovered_report)

    async def recover_after_restart():
        replay = await automation.start_automation_research(request, store=reopened)
        task = automation._background_tasks[
            automation._background_task_key(reopened, request.request_id)
        ]
        await task
        return replay

    replay = asyncio.run(recover_after_restart())
    recovered_receipt = reopened.get(request.request_id)

    assert replay.status_code == 202
    assert replay.body["idempotent_readback"] is True
    assert replay.body["research_id"] == first.body["research_id"]
    assert recovered_receipt["status"] == "completed"
    assert recovered_receipt["lease_generation"] == 2
    assert recovered_receipt["lease_token"] != interrupted_receipt["lease_token"]
    assert recovered_receipt["request"]["request_id"] == request.request_id


def test_async_admission_is_bounded_durable_and_replays_bypass_saturation(
    monkeypatch, tmp_path
):
    capacity = automation.AutomationCapacity(max_concurrent=1, max_queued=1)
    monkeypatch.setattr(automation, "_automation_capacity", lambda: capacity)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    first_payload = request_payload(request_id="admission:first")
    first_payload["query"] = "first bounded operation"
    second_payload = request_payload(request_id="admission:second")
    second_payload["query"] = "second durable queued operation"
    third_payload = request_payload(request_id="admission:third")
    third_payload["query"] = "third operation beyond the bounded queue"
    first = automation.AutomationResearchRequest.model_validate(first_payload)
    second = automation.AutomationResearchRequest.model_validate(second_payload)
    third = automation.AutomationResearchRequest.model_validate(third_payload)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_finished = asyncio.Event()
    calls: list[str] = []

    async def bounded_deep(query, *_args, **_kwargs):
        calls.append(query)
        if query == first.query:
            first_started.set()
            await release_first.wait()
        else:
            second_finished.set()
        return successful_deep_result()

    async def fake_report(*_args, **_kwargs):
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", bounded_deep)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)

    async def scenario():
        first_start = await automation.start_automation_research(first, store=store)
        await first_started.wait()
        first_task = automation._background_tasks[
            automation._background_task_key(store, first.request_id)
        ]
        first_replay = await automation.start_automation_research(first, store=store)
        second_start = await automation.start_automation_research(second, store=store)
        assert store.get(second.request_id)["core_run_id"] is None
        second_replay = await automation.start_automation_research(second, store=store)
        third_start = await automation.start_automation_research(third, store=store)
        assert len(
            [task for task in automation._background_tasks.values() if not task.done()]
        ) == 1
        release_first.set()
        await first_task
        await asyncio.wait_for(second_finished.wait(), timeout=0.5)
        second_task = automation._background_tasks[
            automation._background_task_key(store, second.request_id)
        ]
        await second_task
        return (
            first_start,
            first_replay,
            second_start,
            second_replay,
            third_start,
        )

    first_start, first_replay, second_start, second_replay, third_start = (
        asyncio.run(scenario())
    )

    assert first_start.body["status"] == "running"
    assert first_replay.body["status"] == "running"
    assert first_replay.body["idempotent_readback"] is True
    assert second_start.status_code == 202
    assert second_start.body["status"] == "queued"
    assert second_start.body["idempotent_readback"] is False
    assert second_start.headers == {"Retry-After": "10"}
    assert second_replay.body["status"] == "queued"
    assert second_replay.body["idempotent_readback"] is True
    assert third_start.status_code == 429
    assert third_start.body["status"] == "saturated"
    assert third_start.body["error_code"] == "automation_admission_saturated"
    assert store.get(third.request_id) is None
    assert store.get(second.request_id)["status"] == "completed"
    second_receipt = store.get(second.request_id)
    assert second_receipt["core_run_id"] == automation.attempt_research_id(
        second_receipt["research_id"],
        second_receipt["lease_generation"],
        second_receipt["lease_token"],
    )
    assert calls == [first.query, second.query]


def test_new_arrival_cannot_bypass_older_queued_work_when_a_slot_opens(tmp_path):
    capacity = automation.AutomationCapacity(max_concurrent=1, max_queued=2)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")

    def reserve(request_id: str, query: str):
        payload = request_payload(request_id=request_id)
        payload["query"] = query
        request = automation.AutomationResearchRequest.model_validate(payload)
        fingerprint = automation.request_fingerprint(request)
        row, created = store.reserve(
            request.request_id,
            fingerprint,
            automation.deterministic_research_id(request.request_id, fingerprint),
            request_payload=request.model_dump(mode="json", exclude_none=True),
            capacity=capacity,
        )
        return request, fingerprint, row, created

    first, first_fingerprint, first_row, _ = reserve(
        "fairness:running", "current running work"
    )
    older, _, older_row, _ = reserve(
        "fairness:older", "older durable queued work"
    )
    assert first_row["status"] == "running"
    assert older_row["status"] == "queued"

    store.finish(
        first.request_id,
        first_fingerprint,
        first_row["lease_token"],
        {
            "status": "failed",
            "error_code": "test_owner_finished",
            "error_message": "The running slot is now free",
            "completed_at": first_row["started_at"],
        },
    )

    newcomer, newcomer_fingerprint, newcomer_row, created = reserve(
        "fairness:newcomer", "new work arriving after the slot opens"
    )
    assert created is True
    assert newcomer_row["status"] == "queued"
    assert newcomer_row["core_run_id"] is None

    replay, replay_fingerprint, replay_row, replay_created = reserve(
        newcomer.request_id, newcomer.query
    )
    assert replay == newcomer
    assert replay_fingerprint == newcomer_fingerprint
    assert replay_created is False
    assert replay_row["request_id"] == newcomer.request_id

    promoted = store.claim_next_queued(capacity=capacity)
    assert promoted is not None
    assert promoted["request_id"] == older.request_id
    assert promoted["status"] == "running"
    assert store.get(newcomer.request_id)["status"] == "queued"


def test_replaying_newer_queued_work_cannot_bypass_fifo_order(
    monkeypatch, tmp_path
):
    capacity = automation.AutomationCapacity(max_concurrent=1, max_queued=2)
    monkeypatch.setattr(automation, "_automation_capacity", lambda: capacity)
    monkeypatch.setattr(automation, "_queue_recovery_poll_seconds", lambda: 0.005)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")

    def reserve(request_id: str, query: str):
        payload = request_payload(request_id=request_id)
        payload["query"] = query
        request = automation.AutomationResearchRequest.model_validate(payload)
        fingerprint = automation.request_fingerprint(request)
        row, _ = store.reserve(
            request.request_id,
            fingerprint,
            automation.deterministic_research_id(request.request_id, fingerprint),
            request_payload=request.model_dump(mode="json", exclude_none=True),
            capacity=capacity,
        )
        return request, fingerprint, row

    owner, owner_fingerprint, owner_row = reserve("fifo:owner", "running owner")
    older, _, older_row = reserve("fifo:older", "oldest queued work")
    newer, _, newer_row = reserve("fifo:newer", "newer queued work")
    assert older_row["status"] == newer_row["status"] == "queued"
    store.finish(
        owner.request_id,
        owner_fingerprint,
        owner_row["lease_token"],
        {
            "status": "failed",
            "error_code": "test_owner_finished",
            "error_message": "The running slot is now free",
            "completed_at": owner_row["started_at"],
        },
    )
    older_started = asyncio.Event()
    release_older = asyncio.Event()

    async def deep(query, *_args, **_kwargs):
        if query == older.query:
            older_started.set()
            await release_older.wait()
        return successful_deep_result()

    async def report(*_args, **_kwargs):
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", deep)
    monkeypatch.setattr(automation, "write_report_tool", report)

    async def scenario():
        replay = await automation.start_automation_research(newer, store=store)
        assert replay.body["status"] == "queued"
        await asyncio.wait_for(older_started.wait(), timeout=1)
        assert store.get(older.request_id)["status"] == "running"
        assert store.get(newer.request_id)["status"] == "queued"
        release_older.set()
        for _ in range(100):
            if store.get(newer.request_id)["status"] == "completed":
                return
            await asyncio.sleep(0.005)
        raise AssertionError("FIFO follower did not complete")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("deadlines", "slow_phase", "expected_code"),
    [
        (
            automation.AutomationDeadlines(1, 0.01, 1),
            "deep",
            "automation_deep_research_timeout",
        ),
        (
            automation.AutomationDeadlines(1, 1, 0.01),
            "report",
            "automation_report_timeout",
        ),
        (
            automation.AutomationDeadlines(0.01, 1, 1),
            "deep",
            "automation_overall_timeout",
        ),
    ],
)
def test_async_deadlines_persist_typed_terminal_timeouts(
    monkeypatch, tmp_path, deadlines, slow_phase, expected_code
):
    request = automation.AutomationResearchRequest.model_validate(
        request_payload(request_id=f"timeout:{expected_code}")
    )
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    monkeypatch.setattr(automation, "_automation_deadlines", lambda: deadlines)

    core_ids: list[str] = []

    async def deep(*_args, **kwargs):
        core_id = kwargs["_research_id"]
        core_ids.append(core_id)
        core = ResearchRunStore(store.path, recover_interrupted=False)
        core.create_run(
            core_id,
            query=request.query,
            report_type=request.report_type,
            report_source=request.report_source,
            tone=request.tone,
        )
        if slow_phase == "deep":
            await asyncio.sleep(1)
        core.complete_run(
            core_id,
            context="deadline test evidence",
            sources=[],
            source_urls=["https://authority.example/evidence"],
            source_manifest=successful_deep_result()["source_manifest"],
        )
        return successful_deep_result()

    async def report(*_args, **_kwargs):
        if slow_phase == "report":
            await asyncio.sleep(1)
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", deep)
    monkeypatch.setattr(automation, "write_report_tool", report)

    async def scenario():
        await automation.start_automation_research(request, store=store)
        task = automation._background_tasks[
            automation._background_task_key(store, request.request_id)
        ]
        await task

    asyncio.run(scenario())
    receipt = store.get(request.request_id)
    result = automation.read_automation_research_result(request.request_id, store=store)

    assert receipt["status"] == "failed"
    assert receipt["overall_timeout_seconds"] == deadlines.overall_seconds
    assert receipt["deep_timeout_seconds"] == deadlines.deep_research_seconds
    assert receipt["report_timeout_seconds"] == deadlines.report_seconds
    assert result.status_code == 200
    assert result.body["error_code"] == expected_code
    assert result.body["publishable"] is False
    assert result.body["k2_handoff"] is None
    core = ResearchRunStore(store.path, recover_interrupted=False).get_run(core_ids[0])
    assert core["status"] == "failed"
    assert core["error_code"] == expected_code


def test_reclaimed_attempt_uses_distinct_core_identity_and_fences_old_writer(
    monkeypatch, tmp_path
):
    request = automation.AutomationResearchRequest.model_validate(
        request_payload(request_id="attempt:fencing")
    )
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    core_ids: list[str] = []

    async def deep(*_args, **kwargs):
        core_id = kwargs["_research_id"]
        core_ids.append(core_id)
        core = ResearchRunStore(store.path, recover_interrupted=False)
        core.create_run(
            core_id,
            query=request.query,
            report_type=request.report_type,
            report_source=request.report_source,
            tone=request.tone,
        )
        if len(core_ids) == 1:
            first_started.set()
            await release_first.wait()
        core.complete_run(
            core_id,
            context="attempt evidence",
            sources=[],
            source_urls=["https://authority.example/evidence"],
            source_manifest=successful_deep_result()["source_manifest"],
        )
        return successful_deep_result()

    async def report(*_args, **_kwargs):
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", deep)
    monkeypatch.setattr(automation, "write_report_tool", report)
    monkeypatch.setattr(automation, "_stale_cutoff_iso", lambda: "9999-01-01")

    async def scenario():
        stale_task = asyncio.create_task(
            automation.execute_automation_research(request, store=store)
        )
        await first_started.wait()
        winner = await automation.execute_automation_research(request, store=store)
        release_first.set()
        stale = await stale_task
        return winner, stale

    winner, stale = asyncio.run(scenario())
    receipt = store.get(request.request_id)
    terminal = automation.read_automation_research_result(request.request_id, store=store)

    assert len(core_ids) == 2
    assert core_ids[0] != core_ids[1]
    assert winner.body["core_run_id"] == core_ids[1]
    assert receipt["core_run_id"] == core_ids[1]
    assert receipt["lease_generation"] == 2
    assert receipt["core_run_id"] == automation.attempt_research_id(
        receipt["research_id"], receipt["lease_generation"], receipt["lease_token"]
    )
    assert winner.body["attempt_started_at"] == receipt["attempt_started_at"]
    assert terminal.body["upstream_execution"]["startedAt"] == receipt[
        "attempt_started_at"
    ]
    assert terminal.body["provenance"]["operation_started_at"] == receipt[
        "started_at"
    ]
    assert terminal.body["provenance"]["attempt_started_at"] == receipt[
        "attempt_started_at"
    ]
    assert stale.body["idempotent_readback"] is True
    assert ResearchRunStore(store.path, recover_interrupted=False).get_run(core_ids[0])
    assert ResearchRunStore(store.path, recover_interrupted=False).get_run(core_ids[1])


def _terminal_existing(report: str, *, report_prompt: str | None = None):
    payload = request_payload(request_id="handoff:boundary")
    if report_prompt is not None:
        payload["report_prompt"] = report_prompt
    request = automation.AutomationResearchRequest.model_validate(payload)
    fingerprint = automation.request_fingerprint(request)
    research_id = automation.deterministic_research_id(request.request_id, fingerprint)
    report_result = successful_report_result()
    report_result["report"] = report
    response = automation._terminal_response(
        request,
        fingerprint,
        research_id,
        started_at="2026-08-30T12:00:00+00:00",
        deep_result=successful_deep_result(),
        report_result=report_result,
    )
    return {
        "request_id": request.request_id,
        "request_fingerprint": fingerprint,
        "research_id": research_id,
        "core_run_id": research_id,
        "lease_generation": 1,
        "status": response["status"],
        "response": response,
        "request": request.model_dump(mode="json", exclude_none=True),
        "started_at": response["started_at"],
        "completed_at": response["completed_at"],
    }


@pytest.mark.parametrize(
    ("report", "expected_status"),
    [
        ("a" * 128_000, "ready"),
        ("é" * 64_000, "ready"),
        ("é" * 64_000 + "a", "withheld"),
        ("a" * 128_001, "withheld"),
    ],
)
def test_k2_handoff_honors_character_and_utf8_byte_boundaries(
    report, expected_status
):
    readback = automation._terminal_result_readback(_terminal_existing(report))
    handoff = readback["k2_handoff"]

    assert handoff["status"] == expected_status
    if expected_status == "ready":
        assert handoff["arguments"]["content"] == report
        assert len(report) <= automation.K2_INLINE_MAX_CHARS
        assert len(report.encode("utf-8")) <= automation.K2_INLINE_MAX_BYTES
    else:
        assert "arguments" not in handoff
        assert handoff["reason"]["code"] == "k2_inline_content_limit_exceeded"
        assert handoff["locator"].startswith("research-run:")


def test_terminal_contract_manifest_snapshot_and_full_normalized_brief_are_exact():
    long_prompt = "  " + ("Use this nuance. " * 900) + "  "
    existing = _terminal_existing("Accepted durable report.", report_prompt=long_prompt)
    result = automation._terminal_result_readback(existing)
    canonical = json.dumps(
        automation.ASYNC_CONTRACT_DESCRIPTOR,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_fingerprint = "sha256:" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()

    assert automation.ASYNC_CONTRACT_FINGERPRINT == expected_fingerprint
    assert result["contract_manifest"] == {
        "schemaVersion": "mastery_research_contract_manifest.v1",
        "canonicalization": "json.sort_keys.compact_utf8.v1",
        "contractFingerprint": expected_fingerprint,
        "descriptor": automation.ASYNC_CONTRACT_DESCRIPTOR,
    }
    assert [
        operation["name"]
        for operation in result["contract_manifest"]["descriptor"]["operations"]
    ] == [
        "mastery_research_start",
        "mastery_research_status",
        "mastery_research_result",
    ]
    assert result["source_snapshot_id"] == result["upstream_execution"][
        "sourceSnapshotId"
    ]
    assert result["provenance"]["query"] == existing["request"]["query"]
    assert result["provenance"]["report_prompt"] == existing["request"][
        "report_prompt"
    ]
    assert result["normalized_request"]["report_prompt"] == existing["request"][
        "report_prompt"
    ]
    mission = result["k2_handoff"]["arguments"]["mission"]
    assert "Bounded research-brief excerpt" in mission
    assert len(mission) <= 4_000
    assert existing["request"]["report_prompt"] not in mission


def test_pure_read_errors_distinguish_not_found_unavailable_and_corrupt(
    monkeypatch, tmp_path
):
    valid_path = tmp_path / "valid.sqlite3"
    store = automation.AutomationResearchStore(valid_path)
    missing_status = automation.read_automation_research_status("missing", store=store)
    missing_result = automation.read_automation_research_result("missing", store=store)
    assert missing_status.status_code == 404
    assert missing_status.body["schema_version"] == "research_automation_status.v1"
    assert missing_status.body["operation"] == "mastery_research_status"
    assert missing_result.status_code == 404
    assert missing_result.body["schema_version"] == "research_automation_result_read.v1"
    assert missing_result.body["operation"] == "mastery_research_result"

    corrupt_path = tmp_path / "corrupt.sqlite3"
    corrupt_path.write_bytes(b"not a sqlite database")
    monkeypatch.setenv("RESEARCH_RUN_STORE_PATH", str(corrupt_path))
    automation.clear_automation_hot_state()
    corrupt_status = automation.read_automation_research_status("missing")
    corrupt_result = automation.read_automation_research_result("missing")
    assert corrupt_status.status_code == 500
    assert corrupt_status.body["error_code"] == "automation_store_corrupt"
    assert corrupt_status.body["schema_version"] == "research_automation_status.v1"
    assert corrupt_result.status_code == 500
    assert corrupt_result.body["error_code"] == "automation_store_corrupt"
    assert corrupt_result.body["schema_version"] == "research_automation_result_read.v1"


def test_invalid_status_and_result_paths_use_their_own_error_schemas():
    status = automation._invalid_path_response("mastery_research_status")
    result = automation._invalid_path_response("mastery_research_result")
    status_body = json.loads(status.body)
    result_body = json.loads(result.body)

    assert status_body["schema_version"] == "research_automation_status.v1"
    assert status_body["operation"] == "mastery_research_status"
    assert result_body["schema_version"] == "research_automation_result_read.v1"
    assert result_body["operation"] == "mastery_research_result"
    assert result_body["contract_manifest"] == automation.ASYNC_CONTRACT_MANIFEST


@pytest.mark.parametrize(
    ("column", "bad_json"),
    [
        ("request_json", "{not-json"),
        ("response_json", "{not-json"),
        ("request_json", ""),
        ("response_json", ""),
    ],
)
def test_malformed_persisted_json_is_a_typed_corrupt_receipt(
    tmp_path, column, bad_json
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    fingerprint = automation.request_fingerprint(request)
    research_id = automation.deterministic_research_id(request.request_id, fingerprint)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    store.reserve(
        request.request_id,
        fingerprint,
        research_id,
        request_payload=request.model_dump(mode="json", exclude_none=True),
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            f"UPDATE automation_research_requests SET {column} = ? WHERE request_id = ?",
            (bad_json, request.request_id),
        )

    status = automation.read_automation_research_status(request.request_id, store=store)
    result = automation.read_automation_research_result(request.request_id, store=store)

    assert status.status_code == 500
    assert status.body["schema_version"] == "research_automation_status.v1"
    assert status.body["error_code"] == "idempotency_receipt_corrupt"
    assert result.status_code == 500
    assert result.body["schema_version"] == "research_automation_result_read.v1"
    assert result.body["error_code"] == "idempotency_receipt_corrupt"


def test_queue_recovers_abandoned_slot_after_restart_without_replaying_owner(
    monkeypatch, tmp_path
):
    capacity = automation.AutomationCapacity(max_concurrent=1, max_queued=2)
    monkeypatch.setattr(automation, "_automation_capacity", lambda: capacity)
    monkeypatch.setattr(automation, "_queue_recovery_poll_seconds", lambda: 0.005)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    abandoned = automation.AutomationResearchRequest.model_validate(
        request_payload(request_id="restart:abandoned")
    )
    queued_payload = request_payload(request_id="restart:queued")
    queued_payload["query"] = "queued work after an abandoned owner"
    queued = automation.AutomationResearchRequest.model_validate(queued_payload)
    abandoned_fingerprint = automation.request_fingerprint(abandoned)
    store.reserve(
        abandoned.request_id,
        abandoned_fingerprint,
        automation.deterministic_research_id(
            abandoned.request_id, abandoned_fingerprint
        ),
        request_payload=abandoned.model_dump(mode="json", exclude_none=True),
        capacity=capacity,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE automation_research_requests
               SET updated_at = '2000-01-01T00:00:00+00:00'
             WHERE request_id = ?
            """,
            (abandoned.request_id,),
        )
    completed: list[str] = []

    async def deep(query, *_args, **_kwargs):
        completed.append(query)
        return successful_deep_result()

    async def report(*_args, **_kwargs):
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", deep)
    monkeypatch.setattr(automation, "write_report_tool", report)

    async def scenario():
        started = await automation.start_automation_research(queued, store=store)
        assert started.body["status"] == "queued"
        for _ in range(100):
            if store.get(queued.request_id)["status"] == "completed":
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("durable queue did not recover the abandoned slot")

    asyncio.run(scenario())

    assert store.get(abandoned.request_id)["status"] == "completed"
    assert store.get(abandoned.request_id)["lease_generation"] == 2
    assert store.get(queued.request_id)["status"] == "completed"
    assert completed == [abandoned.query, queued.query]


def test_blocking_route_recovers_abandoned_slot_without_async_caller(
    monkeypatch, tmp_path
):
    capacity = automation.AutomationCapacity(max_concurrent=1, max_queued=2)
    monkeypatch.setattr(automation, "_automation_capacity", lambda: capacity)
    monkeypatch.setattr(automation, "_queue_recovery_poll_seconds", lambda: 0.005)
    monkeypatch.setattr(
        automation, "_blocking_admission_poll_seconds", lambda: 0.005
    )
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    abandoned = automation.AutomationResearchRequest.model_validate(
        request_payload(request_id="blocking-restart:abandoned")
    )
    follower_payload = request_payload(request_id="blocking-restart:follower")
    follower_payload["query"] = "blocking follower after an abandoned owner"
    follower = automation.AutomationResearchRequest.model_validate(follower_payload)
    abandoned_fingerprint = automation.request_fingerprint(abandoned)
    store.reserve(
        abandoned.request_id,
        abandoned_fingerprint,
        automation.deterministic_research_id(
            abandoned.request_id, abandoned_fingerprint
        ),
        request_payload=abandoned.model_dump(mode="json", exclude_none=True),
        capacity=capacity,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE automation_research_requests
               SET updated_at = '2000-01-01T00:00:00+00:00'
             WHERE request_id = ?
            """,
            (abandoned.request_id,),
        )
    completed: list[str] = []

    async def deep(query, *_args, **_kwargs):
        completed.append(query)
        return successful_deep_result()

    async def report(*_args, **_kwargs):
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", deep)
    monkeypatch.setattr(automation, "write_report_tool", report)

    result = asyncio.run(
        asyncio.wait_for(
            automation.execute_automation_research(follower, store=store),
            timeout=1,
        )
    )

    assert result.status_code == 200
    assert result.body["status"] == "completed"
    assert store.get(abandoned.request_id)["status"] == "completed"
    assert store.get(abandoned.request_id)["lease_generation"] == 2
    assert store.get(follower.request_id)["status"] == "completed"
    assert completed == [abandoned.query, follower.query]


def test_boot_recovery_keeps_supervising_a_fresh_lone_operation_until_stale(
    monkeypatch, tmp_path
):
    path = tmp_path / "runs.sqlite3"
    monkeypatch.setenv("RESEARCH_RUN_STORE_PATH", str(path))
    monkeypatch.setattr(automation, "_queue_recovery_poll_seconds", lambda: 0.005)
    monkeypatch.setattr(
        automation,
        "_automation_capacity",
        lambda: automation.AutomationCapacity(max_concurrent=1, max_queued=2),
    )
    stale_cutoff = {"value": "2000-01-01T00:00:00+00:00"}
    monkeypatch.setattr(
        automation, "_stale_cutoff_iso", lambda: stale_cutoff["value"]
    )
    request = automation.AutomationResearchRequest.model_validate(
        request_payload(request_id="boot-recovery:lone-stale")
    )
    store = automation.AutomationResearchStore(path)
    fingerprint = automation.request_fingerprint(request)
    store.reserve(
        request.request_id,
        fingerprint,
        automation.deterministic_research_id(request.request_id, fingerprint),
        request_payload=request.model_dump(mode="json", exclude_none=True),
        capacity=automation.AutomationCapacity(max_concurrent=1, max_queued=2),
    )
    async def deep(*_args, **_kwargs):
        return successful_deep_result()

    async def report(*_args, **_kwargs):
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", deep)
    monkeypatch.setattr(automation, "write_report_tool", report)
    automation.clear_automation_hot_state()

    async def scenario():
        automation.schedule_automation_research_recovery()
        await asyncio.sleep(0.02)
        fresh = store.get(request.request_id)
        assert fresh["status"] == "running"
        assert fresh["lease_generation"] == 1
        stale_cutoff["value"] = "9999-01-01T00:00:00+00:00"
        for _ in range(100):
            receipt = store.get(request.request_id)
            if receipt["status"] == "completed":
                return receipt
            await asyncio.sleep(0.005)
        raise AssertionError("boot recovery did not reclaim the lone stale operation")

    recovered = asyncio.run(scenario())

    assert recovered["status"] == "completed"
    assert recovered["lease_generation"] == 2
    assert recovered["attempt_started_at"] != recovered["started_at"]


def test_heartbeat_failure_is_typed_terminal_and_does_not_escape_background_task(
    monkeypatch, tmp_path
):
    request = automation.AutomationResearchRequest.model_validate(
        request_payload(request_id="heartbeat:failure")
    )
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    core_ids: list[str] = []

    async def deep(*_args, **kwargs):
        core_id = kwargs["_research_id"]
        core_ids.append(core_id)
        core = ResearchRunStore(store.path, recover_interrupted=False)
        core.create_run(
            core_id,
            query=request.query,
            report_type=request.report_type,
            report_source=request.report_source,
            tone=request.tone,
        )
        await asyncio.sleep(0.03)
        core.complete_run(core_id, context="evidence", sources=[], source_urls=[])
        return successful_deep_result()

    async def report(*_args, **_kwargs):
        return successful_report_result()

    def broken_heartbeat(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated lease-store failure")

    monkeypatch.setattr(automation, "_heartbeat_interval_seconds", lambda: 0.005)
    monkeypatch.setattr(store, "heartbeat", broken_heartbeat)
    monkeypatch.setattr(automation, "deep_research_tool", deep)
    monkeypatch.setattr(automation, "write_report_tool", report)

    async def scenario():
        await automation.start_automation_research(request, store=store)
        task = automation._background_tasks[
            automation._background_task_key(store, request.request_id)
        ]
        completed_result = await task
        assert completed_result.status_code == 200

    asyncio.run(scenario())
    receipt = store.get(request.request_id)
    core = ResearchRunStore(store.path, recover_interrupted=False).get_run(core_ids[0])

    assert receipt["status"] == "failed"
    assert receipt["response"]["error_code"] == "automation_lease_heartbeat_failed"
    assert core["status"] == "failed"
    assert core["error_code"] == "automation_lease_heartbeat_failed"


def test_k2_title_and_mission_are_bounded_in_utf16_units():
    payload = request_payload(request_id="handoff:utf16")
    payload["query"] = "😀" * 1_000
    payload["report_prompt"] = "🧠" * 2_000
    request = automation.AutomationResearchRequest.model_validate(payload)
    fingerprint = automation.request_fingerprint(request)
    research_id = automation.deterministic_research_id(request.request_id, fingerprint)
    report_result = successful_report_result()
    response = automation._terminal_response(
        request,
        fingerprint,
        research_id,
        started_at="2026-08-30T12:00:00+00:00",
        deep_result=successful_deep_result(),
        report_result=report_result,
    )
    existing = {
        "request_id": request.request_id,
        "request_fingerprint": fingerprint,
        "research_id": research_id,
        "core_run_id": research_id,
        "lease_generation": 1,
        "status": "completed",
        "response": response,
        "request": request.model_dump(mode="json", exclude_none=True),
        "started_at": response["started_at"],
        "completed_at": response["completed_at"],
    }
    arguments = automation._terminal_result_readback(existing)["k2_handoff"][
        "arguments"
    ]

    assert automation._utf16_units(arguments["title"]) <= 1_000
    assert automation._utf16_units(arguments["mission"]) <= 4_000
    assert arguments["title"].endswith("…")
    assert "Bounded research-brief excerpt" in arguments["mission"]


def test_terminal_readback_replays_frozen_contract_and_snapshot(monkeypatch):
    existing = _terminal_existing("Frozen provider output.")
    frozen_contract = deepcopy(existing["response"]["contract_manifest"])
    frozen_snapshot_id = existing["response"]["source_snapshot_id"]
    monkeypatch.setattr(
        automation,
        "ASYNC_CONTRACT_MANIFEST",
        {
            "schemaVersion": "future-contract.v9",
            "contractFingerprint": f"sha256:{'0' * 64}",
            "descriptor": {},
        },
    )

    result = automation._terminal_result_readback(existing)

    assert result["contract_manifest"] == frozen_contract
    assert result["source_snapshot_id"] == frozen_snapshot_id
    assert result["upstream_execution"]["contractFingerprint"] == frozen_contract[
        "contractFingerprint"
    ]
    assert result["upstream_execution"]["sourceSnapshotId"] == frozen_snapshot_id


@pytest.mark.parametrize(
    "mutate",
    [
        lambda response: response.clear(),
        lambda response: response.update(request_id="different-request"),
        lambda response: response.update(request_fingerprint="0" * 64),
        lambda response: response.update(completed_at=None),
    ],
)
def test_structurally_invalid_terminal_receipts_fail_closed(
    tmp_path, mutate
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    fingerprint = automation.request_fingerprint(request)
    research_id = automation.deterministic_research_id(request.request_id, fingerprint)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    row, created = store.reserve(
        request.request_id,
        fingerprint,
        research_id,
        request_payload=request.model_dump(mode="json", exclude_none=True),
    )
    assert created is True
    response = automation._terminal_response(
        request,
        fingerprint,
        research_id,
        started_at=row["started_at"],
        deep_result=successful_deep_result(),
        report_result=successful_report_result(),
    )
    mutate(response)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE automation_research_requests
               SET status = 'completed', response_json = ?, completed_at = ?
             WHERE request_id = ?
            """,
            (
                json.dumps(response),
                response.get("completed_at"),
                request.request_id,
            ),
        )

    status = automation.read_automation_research_status(request.request_id, store=store)
    result = automation.read_automation_research_result(request.request_id, store=store)

    assert status.status_code == 500
    assert status.body["error_code"] == "idempotency_receipt_corrupt"
    assert result.status_code == 500
    assert result.body["error_code"] == "idempotency_receipt_corrupt"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda response: response["contract_manifest"].update(
            contractFingerprint=f"sha256:{'0' * 64}"
        ),
        lambda response: response.update(
            source_snapshot_id=f"mastery-research:tampered@sha256:{'0' * 64}"
        ),
        lambda response: response["source_snapshot_manifest"]["report"].update(
            sha256=f"sha256:{'0' * 64}"
        ),
        lambda response: response.update(report="Tampered after terminalization."),
        lambda response: response.update(source_urls=["https://tampered.example/"]),
        lambda response: (
            response.pop("contract_manifest"),
            response.pop("source_snapshot_manifest"),
            response.pop("source_snapshot_id"),
        ),
    ],
)
def test_frozen_contract_and_source_snapshot_tampering_fails_closed(
    tmp_path, tamper
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    fingerprint = automation.request_fingerprint(request)
    research_id = automation.deterministic_research_id(request.request_id, fingerprint)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    row, _ = store.reserve(
        request.request_id,
        fingerprint,
        research_id,
        request_payload=request.model_dump(mode="json", exclude_none=True),
    )
    response = automation._terminal_response(
        request,
        fingerprint,
        research_id,
        started_at=row["started_at"],
        core_run_id=row["core_run_id"],
        lease_generation=row["lease_generation"],
        attempt_started_at=row["attempt_started_at"],
        deadline_at=row["deadline_at"],
        deep_result=successful_deep_result(),
        report_result=successful_report_result(),
    )
    tamper(response)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE automation_research_requests
               SET status = 'completed', response_json = ?, completed_at = ?
             WHERE request_id = ?
            """,
            (json.dumps(response), response["completed_at"], request.request_id),
        )

    status = automation.read_automation_research_status(request.request_id, store=store)
    result = automation.read_automation_research_result(request.request_id, store=store)

    assert status.status_code == 500
    assert status.body["error_code"] == "idempotency_receipt_corrupt"
    assert result.status_code == 500
    assert result.body["error_code"] == "idempotency_receipt_corrupt"


def test_explicitly_migrated_legacy_terminal_receipt_keeps_compatibility(
    tmp_path,
):
    request = automation.AutomationResearchRequest.model_validate(request_payload())
    fingerprint = automation.request_fingerprint(request)
    research_id = automation.deterministic_research_id(request.request_id, fingerprint)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    row, _ = store.reserve(
        request.request_id,
        fingerprint,
        research_id,
        request_payload=request.model_dump(mode="json", exclude_none=True),
    )
    response = automation._terminal_response(
        request,
        fingerprint,
        research_id,
        started_at=row["started_at"],
        core_run_id=row["core_run_id"],
        lease_generation=row["lease_generation"],
        attempt_started_at=row["attempt_started_at"],
        deadline_at=row["deadline_at"],
        deep_result=successful_deep_result(),
        report_result=successful_report_result(),
    )
    response.pop("contract_manifest")
    response.pop("source_snapshot_manifest")
    response.pop("source_snapshot_id")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE automation_research_requests
               SET status = 'completed', response_json = ?, completed_at = ?,
                   receipt_schema_version = ?
             WHERE request_id = ?
            """,
            (
                json.dumps(response),
                response["completed_at"],
                automation.LEGACY_DURABLE_RECEIPT_SCHEMA_VERSION,
                request.request_id,
            ),
        )

    result = automation.read_automation_research_result(
        request.request_id, store=store
    )

    assert result.status_code == 200
    assert result.body["contract_manifest"]["lineageBasis"] == (
        "legacy_current_derived"
    )
    assert result.body["k2_handoff"]["status"] == "ready"


def test_queue_reconciles_terminal_core_before_running_follower(
    monkeypatch, tmp_path
):
    capacity = automation.AutomationCapacity(max_concurrent=1, max_queued=2)
    monkeypatch.setattr(automation, "_automation_capacity", lambda: capacity)
    monkeypatch.setattr(automation, "_stale_cutoff_iso", lambda: "9999-01-01")
    monkeypatch.setattr(automation, "_queue_recovery_poll_seconds", lambda: 0.005)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    crashed = automation.AutomationResearchRequest.model_validate(
        request_payload(request_id="restart:after-report")
    )
    follower_payload = request_payload(request_id="restart:follower")
    follower_payload["query"] = "follower after reconciled terminal report"
    follower = automation.AutomationResearchRequest.model_validate(follower_payload)
    fingerprint = automation.request_fingerprint(crashed)
    research_id = automation.deterministic_research_id(crashed.request_id, fingerprint)
    crashed_row, _ = store.reserve(
        crashed.request_id,
        fingerprint,
        research_id,
        request_payload=crashed.model_dump(mode="json", exclude_none=True),
        capacity=capacity,
    )
    accepted_path = tmp_path / "already-accepted.md"
    accepted_path.write_text(successful_report_result()["report"], encoding="utf-8")
    core = ResearchRunStore(store.path, recover_interrupted=False)
    core.create_run(
        crashed_row["core_run_id"],
        query=crashed.query,
        report_type=crashed.report_type,
        report_source=crashed.report_source,
        tone=crashed.tone,
    )
    core.complete_run(
        crashed_row["core_run_id"],
        context="already researched",
        sources=[
            {
                "title": "Authority",
                "url": "https://authority.example/evidence",
                "content": "Evidence " * 20,
            }
        ],
        source_urls=["https://authority.example/evidence"],
        report_path=str(accepted_path),
        source_manifest=successful_deep_result()["source_manifest"],
        report_quality=successful_report_result()["report_quality"],
    )
    calls = {"deep": 0, "report": 0}

    async def deep(*_args, **_kwargs):
        calls["deep"] += 1
        return successful_deep_result()

    async def report(*_args, **_kwargs):
        calls["report"] += 1
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", deep)
    monkeypatch.setattr(automation, "write_report_tool", report)

    async def scenario():
        start = await automation.start_automation_research(follower, store=store)
        assert start.body["status"] == "queued"
        for _ in range(100):
            if store.get(follower.request_id)["status"] == "completed":
                return
            await asyncio.sleep(0.005)
        raise AssertionError("follower did not run after terminal reconciliation")

    asyncio.run(scenario())
    crashed_result = automation.read_automation_research_result(
        crashed.request_id, store=store
    )

    assert crashed_result.body["report"] == accepted_path.read_text(encoding="utf-8")
    assert crashed_result.body["core_run_id"] == crashed_row["core_run_id"]
    assert store.get(crashed.request_id)["lease_generation"] == 2
    assert store.get(follower.request_id)["status"] == "completed"
    assert calls == {"deep": 1, "report": 1}


def test_blocking_and_async_calls_share_the_same_bounded_admission_pool(
    monkeypatch, tmp_path
):
    capacity = automation.AutomationCapacity(max_concurrent=1, max_queued=1)
    monkeypatch.setattr(automation, "_automation_capacity", lambda: capacity)
    monkeypatch.setattr(automation, "_queue_recovery_poll_seconds", lambda: 0.005)
    store = automation.AutomationResearchStore(tmp_path / "runs.sqlite3")
    blocking_payload = request_payload(request_id="mixed:blocking")
    blocking_payload["query"] = "blocking owner"
    queued_payload = request_payload(request_id="mixed:async")
    queued_payload["query"] = "async follower"
    rejected_payload = request_payload(request_id="mixed:rejected")
    rejected_payload["query"] = "beyond shared capacity"
    blocking = automation.AutomationResearchRequest.model_validate(blocking_payload)
    queued = automation.AutomationResearchRequest.model_validate(queued_payload)
    rejected = automation.AutomationResearchRequest.model_validate(rejected_payload)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def deep(query, *_args, **_kwargs):
        if query == blocking.query:
            first_started.set()
            await release_first.wait()
        return successful_deep_result()

    async def report(*_args, **_kwargs):
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", deep)
    monkeypatch.setattr(automation, "write_report_tool", report)

    async def scenario():
        blocking_task = asyncio.create_task(
            automation.execute_automation_research(blocking, store=store)
        )
        await first_started.wait()
        queued_start = await automation.start_automation_research(queued, store=store)
        rejected_start = await automation.start_automation_research(
            rejected, store=store
        )
        release_first.set()
        await blocking_task
        for _ in range(100):
            if store.get(queued.request_id)["status"] == "completed":
                break
            await asyncio.sleep(0.005)
        return queued_start, rejected_start

    queued_start, rejected_start = asyncio.run(scenario())

    assert queued_start.body["status"] == "queued"
    assert rejected_start.status_code == 429
    assert rejected_start.body["error_code"] == "automation_admission_saturated"
    assert store.get(queued.request_id)["status"] == "completed"
    assert store.get(rejected.request_id) is None


def test_new_receipt_never_reuses_an_orphaned_generation_one_core_row(
    monkeypatch, tmp_path
):
    request = automation.AutomationResearchRequest.model_validate(
        request_payload(request_id="orphan:new-receipt")
    )
    fingerprint = automation.request_fingerprint(request)
    stable_research_id = automation.deterministic_research_id(
        request.request_id, fingerprint
    )
    path = tmp_path / "runs.sqlite3"
    core = ResearchRunStore(path, recover_interrupted=False)
    core.create_run(
        stable_research_id,
        query="old orphan query",
        report_type=request.report_type,
        report_source=request.report_source,
        tone=request.tone,
    )
    core.complete_run(
        stable_research_id,
        context="OLD CONTEXT MUST NOT LEAK",
        sources=[{"url": "https://old.example/", "content": "old"}],
        source_urls=["https://old.example/"],
        report_path="/old/report.md",
        source_manifest={"status": "passed", "old": True},
        report_quality={"status": "passed", "publishable": True},
    )
    store = automation.AutomationResearchStore(path)
    seen_core_ids: list[str] = []

    async def failed_deep(*_args, **kwargs):
        core_id = kwargs["_research_id"]
        seen_core_ids.append(core_id)
        fresh = ResearchRunStore(path, recover_interrupted=False)
        fresh.create_run(
            core_id,
            query=request.query,
            report_type=request.report_type,
            report_source=request.report_source,
            tone=request.tone,
        )
        row = fresh.get_run(core_id)
        assert row["context"] == []
        assert row["source_urls"] == []
        assert row["source_manifest"] is None
        assert row["report_quality"] is None
        assert row["report_path"] is None
        assert row["completed_at"] is None
        fresh.fail_run(
            core_id,
            error_code="probe_failure",
            error_message="expected isolated failure",
        )
        return {
            "status": "error",
            "error_code": "probe_failure",
            "message": "expected isolated failure",
        }

    monkeypatch.setattr(automation, "deep_research_tool", failed_deep)

    result = asyncio.run(automation.execute_automation_research(request, store=store))
    receipt = store.get(request.request_id)

    assert result.body["status"] == "failed"
    assert receipt["research_id"] == stable_research_id
    assert receipt["core_run_id"] == seen_core_ids[0]
    assert receipt["core_run_id"] != stable_research_id
    orphan = core.get_run(stable_research_id)
    assert orphan["context"] == "OLD CONTEXT MUST NOT LEAK"
    assert orphan["report_path"] == "/old/report.md"
