import asyncio
import sqlite3
from copy import deepcopy

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
    assert calls[0][5]["_research_id"] == expected_id
    assert calls[0][5]["source_policy"]["enforcement"] == "strict"
    assert calls[0][5]["scope"] == "none"
    assert calls[1] == ("report", expected_id, request.report_prompt)
    assert store.get(request.request_id)["response"]["status"] == "completed"


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
    store.reserve(request.request_id, fingerprint, research_id)
    core = ResearchRunStore(path, recover_interrupted=False)
    core.create_run(
        research_id,
        query=request.query,
        report_type=request.report_type,
        report_source=request.report_source,
        tone=request.tone,
        source_policy=request.source_policy.model_dump(exclude_none=True),
    )
    core.complete_run(
        research_id,
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
        assert received_id == research_id
        assert prompt == request.report_prompt
        return successful_report_result()

    monkeypatch.setattr(automation, "deep_research_tool", should_not_research)
    monkeypatch.setattr(automation, "write_report_tool", fake_report)

    result = asyncio.run(automation.execute_automation_research(request, store=store))

    assert result.status_code == 200
    assert result.body["status"] == "completed"
    assert result.body["research_id"] == research_id
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
        research_id,
        query=request.query,
        report_type=request.report_type,
        report_source=request.report_source,
        tone=request.tone,
        source_policy=request.source_policy.model_dump(exclude_none=True),
    )
    restarted = ResearchRunStore(path, recover_interrupted=True)
    interrupted = restarted.get_run(research_id)
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
        ]
    )
    app.add_middleware(BearerAuthMiddleware)
    client = TestClient(app)

    assert client.post("/automation/research/v1", json={}).status_code == 401
    assert (
        client.post(
            "/automation/research/v1",
            json={},
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )
    allowed = client.post(
        "/automation/research/v1",
        json={},
        headers={"Authorization": "Bearer test-token"},
    )
    assert allowed.status_code == 200
    assert allowed.json() == {"status": "reached"}
