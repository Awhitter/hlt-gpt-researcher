"""Authenticated, idempotent HTTP facade for HLT research automations.

The hosted MCP service remains the research authority. This isolated module
offers a blocking compatibility route plus durable start/status/result jobs for
low-code or ordinary HTTP clients that should not hold a connection during deep
research. It composes the existing strict ``deep_research`` and
``write_report`` functions and deliberately has no delivery integrations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from gpt_researcher.research_run_store import (
    INTERRUPTED_ERROR_CODE,
    ResearchRunStore,
    get_research_run_store_path,
    utc_now_iso,
)
from mcp_server.automation_research_contracts import (
    ASYNC_CONTRACT_DESCRIPTOR,
    ASYNC_CONTRACT_FINGERPRINT,
    ASYNC_CONTRACT_MANIFEST,
    ASYNC_JOB_BASE_PATH,
    CONTRACT_DESCRIPTOR_SCHEMA_VERSION,
    CONTRACT_MANIFEST_SCHEMA_VERSION,
    DURABLE_RECEIPT_SCHEMA_VERSION,
    K2_HANDOFF_SCHEMA_VERSION,
    K2_INLINE_MAX_BYTES,
    K2_INLINE_MAX_CHARS,
    LEGACY_DURABLE_RECEIPT_SCHEMA_VERSION,
    OPERATION_SCHEMA_VERSION,
    PROVENANCE_SCHEMA_VERSION,
    REQUEST_SCHEMA_VERSION,
    RESULT_READ_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    STATUS_SCHEMA_VERSION,
    AutomationAdmissionSaturated,
    AutomationCapacity,
    AutomationDeadlineExceeded,
    AutomationDeadlines,
    AutomationHTTPResult,
    AutomationReceiptReadError,
    AutomationResearchRequest,
    StrictSourcePolicyInput,
    _REQUEST_ID_PATTERN,
    _automation_capacity,
    _automation_deadlines,
    _blocking_admission_poll_seconds,
    _queue_recovery_poll_seconds,
    attempt_research_id,
    canonical_request,
    deterministic_research_id,
    request_fingerprint,
)
from mcp_server.automation_research_store import (
    AutomationLeaseLost,
    AutomationResearchStore,
    _clear_automation_research_store_cache,
    _json_dump,
    get_automation_research_store,
)
from mcp_server.tools import deep_research_tool, write_report_tool

logger = logging.getLogger(__name__)


_request_locks: dict[str, asyncio.Lock] = {}
_background_tasks: dict[tuple[str, str], asyncio.Task[AutomationHTTPResult]] = {}
_queue_drainers: dict[str, asyncio.Task[None]] = {}


def clear_automation_hot_state() -> None:
    """Clear process-only adapter state; durable request receipts remain."""

    for task in list(_background_tasks.values()):
        if task.done():
            continue
        try:
            loop = task.get_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(task.cancel)
            elif not loop.is_closed():
                task.cancel()
        except RuntimeError:
            # A closed test/runtime loop already owns cancellation cleanup.
            pass
    for task in list(_queue_drainers.values()):
        if task.done():
            continue
        try:
            loop = task.get_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(task.cancel)
            elif not loop.is_closed():
                task.cancel()
        except RuntimeError:
            pass
    _background_tasks.clear()
    _queue_drainers.clear()
    _request_locks.clear()
    _clear_automation_research_store_cache()


def _request_lock(request_id: str) -> asyncio.Lock:
    lock = _request_locks.get(request_id)
    if lock is None:
        lock = asyncio.Lock()
        _request_locks[request_id] = lock
    return lock


def _stale_seconds() -> int:
    try:
        requested = int(os.getenv("AUTOMATION_RESEARCH_STALE_SECONDS", "3600"))
    except ValueError:
        requested = 3600
    return max(300, requested)


def _stale_cutoff_iso() -> str:
    """Bound abandoned reservations while leaving ample room for deep runs."""

    return (
        datetime.now(timezone.utc) - timedelta(seconds=_stale_seconds())
    ).isoformat()


def _heartbeat_interval_seconds() -> float:
    return max(5.0, min(60.0, _stale_seconds() / 3))


@asynccontextmanager
async def _lease_heartbeat(
    store: AutomationResearchStore,
    request_id: str,
    fingerprint: str,
    lease_token: str,
):
    """Keep a long strict run live while preserving generation fencing."""

    stop = asyncio.Event()
    state: dict[str, Exception | None] = {"error": None}

    async def renew() -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=_heartbeat_interval_seconds()
                )
                return
            except TimeoutError:
                try:
                    owned = await asyncio.to_thread(
                        store.heartbeat,
                        request_id,
                        fingerprint,
                        lease_token,
                    )
                except Exception as exc:  # noqa: BLE001 - reported to executor
                    state["error"] = exc
                    return
                if not owned:
                    return

    task = asyncio.create_task(renew())
    try:
        yield state
    finally:
        stop.set()
        await task


async def _renew_lease_or_raise(
    store: AutomationResearchStore,
    request_id: str,
    fingerprint: str,
    lease_token: str,
) -> None:
    """Fence expensive phase transitions when another worker has reclaimed."""

    if not await asyncio.to_thread(
        store.heartbeat,
        request_id,
        fingerprint,
        lease_token,
    ):
        raise AutomationLeaseLost("Automation request lease is no longer owned")


def _base_response(
    request: AutomationResearchRequest,
    fingerprint: str,
    research_id: str,
    *,
    started_at: str,
    core_run_id: str | None = None,
    lease_generation: int = 1,
    attempt_started_at: str | None = None,
    deadline_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "request_id": request.request_id,
        "request_fingerprint": fingerprint,
        "research_id": research_id,
        "core_run_id": core_run_id or research_id,
        "lease_generation": lease_generation,
        "publishable": False,
        "idempotent_readback": False,
        "error_code": None,
        "error_message": None,
        "report": None,
        "report_path": None,
        "source_count": 0,
        "source_urls": [],
        "source_manifest": None,
        "report_quality": None,
        "image_count": 0,
        "images": [],
        "cost_usd": 0.0,
        "delivery": {"attempted": False},
        "started_at": started_at,
        "operation_started_at": started_at,
        "attempt_started_at": attempt_started_at or started_at,
        "deadline_at": deadline_at,
        "completed_at": None,
    }


def _snapshot_manifest(
    *,
    fingerprint: str,
    research_id: str,
    core_run_id: str,
    lease_generation: int,
    attempt_started_at: str,
    deadline_at: str | None,
    response: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    report = response.get("report")
    report_text = report if isinstance(report, str) else ""
    evidence = {
        "source_count": int(response.get("source_count") or 0),
        "source_urls": response.get("source_urls") or [],
        "source_manifest": response.get("source_manifest"),
        "image_count": int(response.get("image_count") or 0),
        "images": response.get("images") or [],
    }
    manifest = {
        "schema_version": "mastery_research_source_snapshot.v1",
        "canonicalization": "json.sort_keys.compact_utf8.v1",
        "request_fingerprint": _sha256_ref(fingerprint),
        "research_id": research_id,
        "core_run_id": core_run_id,
        "lease_generation": lease_generation,
        "attempt_started_at": attempt_started_at,
        "deadline_at": deadline_at,
        "report": {
            "sha256": (
                "sha256:" + hashlib.sha256(report_text.encode("utf-8")).hexdigest()
                if report_text
                else None
            ),
            "unicode_codepoints": len(report_text),
            "utf8_bytes": len(report_text.encode("utf-8")),
        },
        "evidence_fingerprint": "sha256:"
        + hashlib.sha256(_json_dump(evidence).encode("utf-8")).hexdigest(),
    }
    digest = hashlib.sha256(_json_dump(manifest).encode("utf-8")).hexdigest()
    snapshot_id = f"mastery-research:{research_id}@sha256:{digest}"
    return manifest, snapshot_id


def _terminal_response(
    request: AutomationResearchRequest,
    fingerprint: str,
    research_id: str,
    *,
    started_at: str,
    core_run_id: str | None = None,
    lease_generation: int = 1,
    attempt_started_at: str | None = None,
    deadline_at: str | None = None,
    deep_result: dict[str, Any] | None,
    report_result: dict[str, Any] | None,
    research_run: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    deep_result = deep_result or {}
    report_result = report_result or {}
    research_run = research_run or {}
    manifest = (
        report_result.get("source_manifest")
        or deep_result.get("source_manifest")
        or research_run.get("source_manifest")
    )
    quality = report_result.get("report_quality") or research_run.get("report_quality")
    report = report_result.get("report")
    judgment = (
        quality.get("independent_judgment") if isinstance(quality, dict) else None
    )
    claim_checks = judgment.get("claim_checks") if isinstance(judgment, dict) else None
    report_publishable = report_result.get("publishable") is True
    quality_publishable = (
        isinstance(quality, dict) and quality.get("publishable") is True
    )
    accepted = (
        error_code is None
        and report_result.get("status") == "success"
        and report_publishable
        and quality_publishable
        and isinstance(manifest, dict)
        and manifest.get("status") == "passed"
        and isinstance(quality, dict)
        and quality.get("status") == "passed"
        and quality.get("missing_required_citations") == []
        and quality.get("unadmitted_citations") == []
        and isinstance(judgment, dict)
        and judgment.get("verdict") == "pass"
        and isinstance(claim_checks, list)
        and bool(claim_checks)
        and all(
            isinstance(check, dict)
            and isinstance(check.get("claim"), str)
            and bool(check["claim"].strip())
            and check.get("supported") is True
            and isinstance(check.get("source_urls"), list)
            and bool(check["source_urls"])
            and all(
                isinstance(url, str) and bool(url.strip())
                for url in check["source_urls"]
            )
            for check in claim_checks
        )
        and isinstance(report, str)
        and bool(report.strip())
    )
    resolved_error_code = (
        error_code
        or report_result.get("error_code")
        or deep_result.get("error_code")
        or research_run.get("error_code")
    )
    resolved_error_message = (
        error_message
        or report_result.get("message")
        or deep_result.get("message")
        or research_run.get("error_message")
    )
    if not accepted and not resolved_error_code:
        resolved_error_code = "acceptance_invariant_failed"
        resolved_error_message = (
            "Strict research did not satisfy the publication acceptance contract"
        )
    response = _base_response(
        request,
        fingerprint,
        research_id,
        started_at=started_at,
        core_run_id=core_run_id,
        lease_generation=lease_generation,
        attempt_started_at=attempt_started_at,
        deadline_at=deadline_at,
    )
    response.update(
        {
            "status": "completed" if accepted else "failed",
            "publishable": accepted,
            "error_code": None if accepted else resolved_error_code,
            "error_message": None if accepted else resolved_error_message,
            # Never expose a rejected draft as the automation report.
            "report": report if accepted else None,
            "report_path": report_result.get("report_path")
            or research_run.get("report_path"),
            "source_count": int(
                report_result.get(
                    "source_count",
                    deep_result.get(
                        "source_count", research_run.get("source_count", 0)
                    ),
                )
                or 0
            ),
            "source_urls": list(
                deep_result.get("source_urls") or research_run.get("source_urls") or []
            ),
            "source_manifest": manifest,
            "report_quality": quality,
            "image_count": int(
                report_result.get("image_count", deep_result.get("image_count", 0)) or 0
            ),
            "images": list(
                report_result.get("images")
                or deep_result.get("images")
                or research_run.get("research_images")
                or []
            ),
            "cost_usd": float(
                report_result.get("costs", research_run.get("costs", 0.0)) or 0.0
            ),
            "completed_at": utc_now_iso(),
        }
    )
    snapshot_manifest, snapshot_id = _snapshot_manifest(
        fingerprint=fingerprint,
        research_id=research_id,
        core_run_id=str(core_run_id or research_id),
        lease_generation=lease_generation,
        attempt_started_at=attempt_started_at or started_at,
        deadline_at=deadline_at,
        response=response,
    )
    response["source_snapshot_manifest"] = snapshot_manifest
    response["source_snapshot_id"] = snapshot_id
    response["contract_manifest"] = json.loads(_json_dump(ASYNC_CONTRACT_MANIFEST))
    return response


def _idempotency_conflict(
    request: AutomationResearchRequest,
    fingerprint: str,
    existing: dict[str, Any],
) -> AutomationHTTPResult:
    return AutomationHTTPResult(
        status_code=409,
        body={
            "schema_version": RESULT_SCHEMA_VERSION,
            "request_id": request.request_id,
            "request_fingerprint": fingerprint,
            "status": "failed",
            "research_id": existing["research_id"],
            "publishable": False,
            "idempotent_readback": False,
            "error_code": "idempotency_conflict",
            "error_message": (
                "request_id is already bound to a different canonical request"
            ),
            "delivery": {"attempted": False},
        },
    )


def _existing_result(
    request: AutomationResearchRequest,
    fingerprint: str,
    existing: dict[str, Any],
) -> AutomationHTTPResult:
    if existing["request_fingerprint"] != fingerprint:
        return _idempotency_conflict(request, fingerprint, existing)
    response = existing.get("response")
    if response is not None:
        response = dict(response)
        response["idempotent_readback"] = True
        return AutomationHTTPResult(status_code=200, body=response)
    if existing.get("status") in {"queued", "running"}:
        status = str(existing["status"])
        body = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "request_id": request.request_id,
            "request_fingerprint": fingerprint,
            "status": status,
            "research_id": existing["research_id"],
            "core_run_id": existing.get("core_run_id") or existing["research_id"],
            "publishable": False,
            "idempotent_readback": True,
            "delivery": {"attempted": False},
            "started_at": existing["started_at"],
            "completed_at": None,
        }
        return AutomationHTTPResult(
            status_code=202,
            body=body,
            headers={"Retry-After": "10" if status == "queued" else "5"},
        )
    return AutomationHTTPResult(
        status_code=500,
        body={
            "schema_version": RESULT_SCHEMA_VERSION,
            "request_id": request.request_id,
            "request_fingerprint": fingerprint,
            "status": "failed",
            "research_id": existing["research_id"],
            "publishable": False,
            "idempotent_readback": False,
            "error_code": "idempotency_receipt_corrupt",
            "error_message": "Terminal automation receipt is missing its stored response",
            "delivery": {"attempted": False},
        },
    )


def _admission_saturated(
    request: AutomationResearchRequest,
    fingerprint: str,
    research_id: str,
) -> AutomationHTTPResult:
    return AutomationHTTPResult(
        status_code=429,
        body={
            "schema_version": RESULT_SCHEMA_VERSION,
            "request_id": request.request_id,
            "request_fingerprint": fingerprint,
            "research_id": research_id,
            "status": "saturated",
            "publishable": False,
            "idempotent_readback": False,
            "error_code": "automation_admission_saturated",
            "error_message": (
                "Mastery Research is at its configured running and durable queue limits"
            ),
            "delivery": {"attempted": False},
        },
        headers={"Retry-After": "10"},
    )


def _recover_terminal_core_result(
    request: AutomationResearchRequest,
    fingerprint: str,
    existing: dict[str, Any],
    store: AutomationResearchStore,
    run: dict[str, Any] | None = None,
) -> AutomationHTTPResult | None:
    """Close a receipt left running after the strict core reached a terminal state."""

    core_run_id = str(existing.get("core_run_id") or existing["research_id"])
    run = run or ResearchRunStore(store.path, recover_interrupted=False).get_run(
        core_run_id
    )
    if run is None or run.get("status") not in {"completed", "failed"}:
        return None
    # ResearchRunStore deliberately marks in-flight rows failed at process
    # startup. That is a recovery signal, not a terminal research result. Keep
    # returning 202 until the adapter lease is stale enough to reclaim, then
    # resume/re-run under a new lease below.
    if run.get("error_code") == INTERRUPTED_ERROR_CODE:
        return None
    report_result: dict[str, Any] = {
        "status": "error",
        "publishable": False,
        "error_code": run.get("error_code"),
        "message": run.get("error_message"),
        "report_path": run.get("report_path"),
        "source_count": run.get("source_count", 0),
        "source_manifest": run.get("source_manifest"),
        "report_quality": run.get("report_quality"),
        "image_count": len(run.get("research_images") or []),
        "images": run.get("research_images") or [],
        "costs": run.get("costs", 0.0),
    }
    quality = run.get("report_quality") or {}
    if (
        run.get("status") == "completed"
        and quality.get("publishable") is True
        and run.get("report_path")
    ):
        try:
            report = Path(run["report_path"]).read_text(encoding="utf-8")
        except OSError:
            return None
        report_result.update(
            status="success",
            publishable=True,
            error_code=None,
            message=None,
            report=report,
        )
    elif run.get("status") != "failed":
        # Deep research is completed, but write_report has not accepted or
        # rejected a candidate yet.  Another worker still owns this request.
        return None
    claimed = store.claim_for_reconciliation(
        request.request_id,
        fingerprint,
        expected_lease_token=existing["lease_token"],
    )
    if claimed is None:
        latest = store.get(request.request_id)
        return _existing_result(request, fingerprint, latest or existing)
    response = _terminal_response(
        request,
        fingerprint,
        claimed["research_id"],
        started_at=claimed["started_at"],
        core_run_id=core_run_id,
        lease_generation=int(claimed.get("lease_generation") or 1),
        attempt_started_at=claimed.get("attempt_started_at") or claimed["started_at"],
        deadline_at=claimed.get("deadline_at"),
        deep_result=None,
        report_result=report_result,
        research_run=run,
    )
    try:
        store.finish(
            request.request_id,
            fingerprint,
            claimed["lease_token"],
            response,
        )
    except AutomationLeaseLost:
        latest = store.get(request.request_id)
        return _existing_result(request, fingerprint, latest or claimed)
    response = dict(response)
    response["idempotent_readback"] = True
    return AutomationHTTPResult(status_code=200, body=response)


def _terminal_response_from_core_run(
    request: AutomationResearchRequest,
    fingerprint: str,
    reservation: dict[str, Any],
    run: dict[str, Any],
) -> dict[str, Any] | None:
    """Rebuild a lost adapter receipt from one genuine terminal core run."""

    if run.get("error_code") == INTERRUPTED_ERROR_CODE:
        return None
    report_result: dict[str, Any] = {
        "status": "error",
        "publishable": False,
        "error_code": run.get("error_code"),
        "message": run.get("error_message"),
        "report_path": run.get("report_path"),
        "source_count": run.get("source_count", 0),
        "source_manifest": run.get("source_manifest"),
        "report_quality": run.get("report_quality"),
        "image_count": len(run.get("research_images") or []),
        "images": run.get("research_images") or [],
        "costs": run.get("costs", 0.0),
    }
    quality = run.get("report_quality") or {}
    if (
        run.get("status") == "completed"
        and quality.get("publishable") is True
        and run.get("report_path")
    ):
        try:
            report = Path(run["report_path"]).read_text(encoding="utf-8")
        except OSError:
            return None
        report_result.update(
            status="success",
            publishable=True,
            error_code=None,
            message=None,
            report=report,
        )
    elif run.get("status") != "failed":
        return None
    return _terminal_response(
        request,
        fingerprint,
        reservation["research_id"],
        started_at=reservation["started_at"],
        core_run_id=str(run["research_id"]),
        lease_generation=int(reservation.get("lease_generation") or 1),
        attempt_started_at=run.get("started_at")
        or reservation.get("attempt_started_at")
        or reservation["started_at"],
        deadline_at=reservation.get("deadline_at"),
        deep_result=None,
        report_result=report_result,
        research_run=run,
    )


@dataclass(frozen=True)
class AutomationExecutionClaim:
    """One fenced ownership claim ready to execute outside the short lock."""

    fingerprint: str
    reservation: dict[str, Any]
    core_run: dict[str, Any] | None
    resume_report_only: bool
    idempotent_readback: bool


def _clone_completed_core_run(
    *,
    store_path: Path,
    source_run: dict[str, Any],
    target_research_id: str,
    request: AutomationResearchRequest,
) -> None:
    """Copy accepted deep evidence into a new attempt-fenced core run.

    The new writer never touches the stale attempt's row, while report-only
    recovery can reuse already-paid-for research evidence.
    """

    run_store = ResearchRunStore(store_path, recover_interrupted=False)
    run_store.create_run(
        target_research_id,
        query=request.query,
        report_type=request.report_type,
        report_source=request.report_source,
        tone=request.tone,
        status="running",
        hlt_research_scope=source_run.get("hlt_research_scope"),
        source_policy=source_run.get("source_policy")
        or request.source_policy.model_dump(exclude_none=True),
    )
    run_store.complete_run(
        target_research_id,
        context=source_run.get("context"),
        sources=list(source_run.get("sources") or []),
        source_urls=list(source_run.get("source_urls") or []),
        research_images=list(source_run.get("research_images") or []),
        costs=float(source_run.get("costs") or 0.0),
        hlt_research_scope=source_run.get("hlt_research_scope"),
        source_policy=source_run.get("source_policy")
        or request.source_policy.model_dump(exclude_none=True),
        source_manifest=source_run.get("source_manifest"),
    )


def _fail_core_attempt(
    *,
    store_path: Path,
    core_run_id: str,
    error_code: str,
    error_message: str,
) -> None:
    """Make the unique core attempt agree with its terminal adapter receipt."""

    run_store = ResearchRunStore(store_path, recover_interrupted=False)
    if run_store.get_run(core_run_id) is None:
        return
    run_store.fail_run(
        core_run_id,
        error_code=error_code,
        error_message=error_message,
    )


async def _claim_automation_execution(
    request: AutomationResearchRequest,
    store: AutomationResearchStore,
    *,
    bounded_admission: bool = False,
) -> AutomationExecutionClaim | AutomationHTTPResult:
    """Reserve, reconcile, or reclaim work while holding only a short lock."""

    fingerprint = request_fingerprint(request)
    research_id = deterministic_research_id(request.request_id, fingerprint)
    lock = _request_lock(request.request_id)
    try:
        async with lock:
            resume_report_only = False
            idempotent_readback = False
            reservation: dict[str, Any]
            existing = store.get(request.request_id)
            if existing is not None:
                promoted_now = False
                idempotent_readback = True
                if existing["request_fingerprint"] != fingerprint:
                    return _idempotency_conflict(request, fingerprint, existing)
                if existing.get("status") == "queued":
                    if bounded_admission:
                        # Replay bypasses duplicate admission and saturation,
                        # never the durable FIFO order. The single drainer
                        # promotes the oldest queued row atomically.
                        return _existing_result(request, fingerprint, existing)
                    else:
                        promoted = store.claim_queued(
                            request.request_id,
                            fingerprint,
                            capacity=AutomationCapacity(
                                max_concurrent=2_147_483_647,
                                max_queued=2_147_483_647,
                            ),
                        )
                        existing = promoted or existing
                        promoted_now = promoted is not None
                if existing.get("status") != "running":
                    return _existing_result(request, fingerprint, existing)
                core_run = ResearchRunStore(
                    store.path, recover_interrupted=False
                ).get_run(existing.get("core_run_id") or existing["research_id"])
                if core_run is not None:
                    recovered = _recover_terminal_core_result(
                        request, fingerprint, existing, store, core_run
                    )
                    if recovered is not None:
                        return recovered
                if not promoted_now:
                    claimed = store.claim_stale(
                        request.request_id,
                        fingerprint,
                        expected_lease_token=existing["lease_token"],
                        stale_before=_stale_cutoff_iso(),
                    )
                    if claimed is None:
                        latest = store.get(request.request_id)
                        return _existing_result(request, fingerprint, latest or existing)
                    reservation = claimed
                    resume_report_only = bool(
                        core_run
                        and core_run.get("status") == "completed"
                        and (core_run.get("source_manifest") or {}).get("status")
                        == "passed"
                        and not core_run.get("report_quality")
                    )
                    if not resume_report_only:
                        core_run = None
                else:
                    reservation = existing
                    core_run = None
            else:
                try:
                    reservation, created = store.reserve(
                        request.request_id,
                        fingerprint,
                        research_id,
                        request_payload=request.model_dump(
                            mode="json", exclude_none=True
                        ),
                        capacity=_automation_capacity() if bounded_admission else None,
                        deadlines=_automation_deadlines(),
                    )
                except AutomationAdmissionSaturated:
                    return _admission_saturated(request, fingerprint, research_id)
                if not created:
                    return _existing_result(request, fingerprint, reservation)
                if reservation.get("status") == "queued":
                    queued = _existing_result(request, fingerprint, reservation)
                    queued.body["idempotent_readback"] = False
                    return queued
                core_run = None
            return AutomationExecutionClaim(
                fingerprint=fingerprint,
                reservation=reservation,
                core_run=core_run,
                resume_report_only=resume_report_only,
                idempotent_readback=idempotent_readback,
            )
    finally:
        waiters = getattr(lock, "_waiters", None) or ()
        if (
            _request_locks.get(request.request_id) is lock
            and not lock.locked()
            and not waiters
        ):
            _request_locks.pop(request.request_id, None)


async def _execute_automation_claim(
    request: AutomationResearchRequest,
    store: AutomationResearchStore,
    claim: AutomationExecutionClaim,
    *,
    persist_cancellation: bool,
) -> AutomationHTTPResult:
    """Run one already-fenced claim and persist exactly one terminal receipt."""

    fingerprint = claim.fingerprint
    reservation = claim.reservation
    core_run = claim.core_run
    research_id = reservation["research_id"]
    core_run_id = str(reservation.get("core_run_id") or research_id)
    lease_generation = int(reservation.get("lease_generation") or 1)
    started_at = reservation["started_at"]
    attempt_started_at = reservation.get("attempt_started_at") or started_at
    deadline_at = reservation.get("deadline_at")
    if deadline_at:
        remaining_overall = (
            datetime.fromisoformat(str(deadline_at)) - datetime.now(timezone.utc)
        ).total_seconds()
    else:
        remaining_overall = float(
            reservation.get("overall_timeout_seconds")
            or _automation_deadlines().overall_seconds
        )
    deep_timeout = float(
        reservation.get("deep_timeout_seconds")
        or _automation_deadlines().deep_research_seconds
    )
    report_timeout = float(
        reservation.get("report_timeout_seconds")
        or _automation_deadlines().report_seconds
    )
    deep_result: dict[str, Any] | None = None
    report_result: dict[str, Any] | None = None
    async with _lease_heartbeat(
        store,
        request.request_id,
        fingerprint,
        reservation["lease_token"],
    ) as heartbeat_state:
        try:
            if remaining_overall <= 0:
                raise AutomationDeadlineExceeded("overall", 0)
            try:
                async with asyncio.timeout(remaining_overall):
                    policy = request.source_policy.model_dump(exclude_none=True)
                    if claim.resume_report_only:
                        assert core_run is not None  # guarded by the claim predicate
                        await asyncio.to_thread(
                            _clone_completed_core_run,
                            store_path=store.path,
                            source_run=core_run,
                            target_research_id=core_run_id,
                            request=request,
                        )
                        deep_result = {
                            "status": "success",
                            "source_count": core_run.get("source_count", 0),
                            "source_urls": core_run.get("source_urls") or [],
                            "source_manifest": core_run.get("source_manifest"),
                            "image_count": len(core_run.get("research_images") or []),
                            "images": core_run.get("research_images") or [],
                        }
                    else:
                        try:
                            deep_result = await asyncio.wait_for(
                                deep_research_tool(
                                    request.query,
                                    request.report_type,
                                    request.report_source,
                                    request.tone,
                                    scope=request.scope,
                                    depth=request.depth,
                                    max_sources_per_query=request.max_sources_per_query,
                                    include_generated_images=request.include_generated_images,
                                    source_policy=policy,
                                    _research_id=core_run_id,
                                ),
                                timeout=deep_timeout,
                            )
                        except TimeoutError as exc:
                            raise AutomationDeadlineExceeded(
                                "deep_research", deep_timeout
                            ) from exc
                    await _renew_lease_or_raise(
                        store,
                        request.request_id,
                        fingerprint,
                        reservation["lease_token"],
                    )
                    if deep_result.get("status") == "success":
                        try:
                            report_result = await asyncio.wait_for(
                                write_report_tool(
                                    core_run_id,
                                    request.report_prompt,
                                ),
                                timeout=report_timeout,
                            )
                        except TimeoutError as exc:
                            raise AutomationDeadlineExceeded(
                                "report", report_timeout
                            ) from exc
                    await _renew_lease_or_raise(
                        store,
                        request.request_id,
                        fingerprint,
                        reservation["lease_token"],
                    )
                    research_run = ResearchRunStore(
                        store.path, recover_interrupted=False
                    ).get_run(core_run_id)
                    response = _terminal_response(
                        request,
                        fingerprint,
                        research_id,
                        started_at=started_at,
                        core_run_id=core_run_id,
                        lease_generation=lease_generation,
                        attempt_started_at=attempt_started_at,
                        deadline_at=deadline_at,
                        deep_result=deep_result,
                        report_result=report_result,
                        research_run=research_run,
                    )
            except AutomationDeadlineExceeded:
                raise
            except TimeoutError as exc:
                raise AutomationDeadlineExceeded(
                    "overall", remaining_overall
                ) from exc
        except AutomationLeaseLost:
            latest = store.get(request.request_id)
            return _existing_result(request, fingerprint, latest or reservation)
        except asyncio.CancelledError:
            if not persist_cancellation:
                # Background work is process-scoped but the operation is durable.
                # Leave its receipt running so a later start replay can reclaim
                # the fenced lease rather than freezing a shutdown as final failure.
                raise
            response = _terminal_response(
                request,
                fingerprint,
                research_id,
                started_at=started_at,
                core_run_id=core_run_id,
                lease_generation=lease_generation,
                attempt_started_at=attempt_started_at,
                deadline_at=deadline_at,
                deep_result=deep_result,
                report_result=report_result,
                research_run=ResearchRunStore(
                    store.path, recover_interrupted=False
                ).get_run(core_run_id),
                error_code="adapter_cancelled",
                error_message="Automation research was cancelled before completion",
            )
            await asyncio.to_thread(
                _fail_core_attempt,
                store_path=store.path,
                core_run_id=core_run_id,
                error_code="adapter_cancelled",
                error_message="Automation research was cancelled before completion",
            )
            try:
                store.finish(
                    request.request_id,
                    fingerprint,
                    reservation["lease_token"],
                    response,
                )
            except AutomationLeaseLost:
                pass
            raise
        except AutomationDeadlineExceeded as exc:
            timeout_code = f"automation_{exc.phase}_timeout"
            timeout_message = (
                f"Mastery Research {exc.phase.replace('_', ' ')} phase exceeded "
                f"its configured {exc.timeout_seconds:g}-second deadline"
            )
            await asyncio.to_thread(
                _fail_core_attempt,
                store_path=store.path,
                core_run_id=core_run_id,
                error_code=timeout_code,
                error_message=timeout_message,
            )
            response = _terminal_response(
                request,
                fingerprint,
                research_id,
                started_at=started_at,
                core_run_id=core_run_id,
                lease_generation=lease_generation,
                attempt_started_at=attempt_started_at,
                deadline_at=deadline_at,
                deep_result=deep_result,
                report_result=report_result,
                research_run=ResearchRunStore(
                    store.path, recover_interrupted=False
                ).get_run(core_run_id),
                error_code=timeout_code,
                error_message=timeout_message,
            )
        except Exception as exc:  # noqa: BLE001 - turn runtime errors into durable receipts
            logger.error(
                "automation research failed for request_id=%s: %s",
                request.request_id,
                exc,
                exc_info=True,
            )
            response = _terminal_response(
                request,
                fingerprint,
                research_id,
                started_at=started_at,
                core_run_id=core_run_id,
                lease_generation=lease_generation,
                attempt_started_at=attempt_started_at,
                deadline_at=deadline_at,
                deep_result=deep_result,
                report_result=report_result,
                research_run=ResearchRunStore(
                    store.path, recover_interrupted=False
                ).get_run(core_run_id),
                error_code="adapter_runtime_error",
                error_message=type(exc).__name__,
            )
    heartbeat_error = heartbeat_state["error"]
    if heartbeat_error is not None:
        heartbeat_message = (
            "Mastery Research could not renew its durable execution lease"
        )
        await asyncio.to_thread(
            _fail_core_attempt,
            store_path=store.path,
            core_run_id=core_run_id,
            error_code="automation_lease_heartbeat_failed",
            error_message=heartbeat_message,
        )
        response = _terminal_response(
            request,
            fingerprint,
            research_id,
            started_at=started_at,
            core_run_id=core_run_id,
            lease_generation=lease_generation,
            attempt_started_at=attempt_started_at,
            deadline_at=deadline_at,
            deep_result=deep_result,
            report_result=report_result,
            research_run=ResearchRunStore(
                store.path, recover_interrupted=False
            ).get_run(core_run_id),
            error_code="automation_lease_heartbeat_failed",
            error_message=heartbeat_message,
        )
    try:
        store.finish(
            request.request_id,
            fingerprint,
            reservation["lease_token"],
            response,
        )
    except AutomationLeaseLost:
        latest = store.get(request.request_id)
        return _existing_result(request, fingerprint, latest or reservation)
    return AutomationHTTPResult(status_code=200, body=response)


async def execute_automation_research(
    request: AutomationResearchRequest,
    *,
    store: AutomationResearchStore | None = None,
) -> AutomationHTTPResult:
    """Execute or durably read back the legacy blocking automation request."""

    store = store or get_automation_research_store()
    claim = await _claim_automation_execution(
        request,
        store,
        bounded_admission=True,
    )
    while (
        isinstance(claim, AutomationHTTPResult)
        and claim.status_code == 202
        and claim.body.get("status") == "queued"
    ):
        _schedule_queue_drain(store)
        await asyncio.sleep(_blocking_admission_poll_seconds())
        claim = await _claim_automation_execution(
            request,
            store,
            bounded_admission=True,
        )
    if isinstance(claim, AutomationHTTPResult):
        if claim.status_code == 202 and claim.body.get("status") == "running":
            key = _background_task_key(store, request.request_id)
            local_task = _background_tasks.get(key)
            drainer = _queue_drainers.get(key[0])
            # A FIFO drainer persists the queued -> running transition before
            # registering its local task. Give that bounded, event-loop-local
            # promotion window a chance to close so the legacy blocking route
            # does not leak a transient 202 for work this process owns.
            for _ in range(8):
                if local_task is not None or drainer is None or drainer.done():
                    break
                await asyncio.sleep(0)
                local_task = _background_tasks.get(key)
            if local_task is not None:
                return await local_task
            latest = await asyncio.to_thread(store.get, request.request_id)
            if latest is not None and latest.get("response") is not None:
                return _existing_result(request, request_fingerprint(request), latest)
        return claim
    result = await _execute_automation_claim(
        request,
        store,
        claim,
        persist_cancellation=True,
    )
    _schedule_queue_drain(store)
    return result


def _background_task_key(
    store: AutomationResearchStore, request_id: str
) -> tuple[str, str]:
    return str(store.path.resolve()), request_id


def _operation_urls(request_id: str) -> tuple[str, str]:
    encoded_id = quote(request_id, safe="")
    return (
        f"{ASYNC_JOB_BASE_PATH}/{encoded_id}/status",
        f"{ASYNC_JOB_BASE_PATH}/{encoded_id}/result",
    )


def _operation_receipt(
    *,
    request_id: str,
    fingerprint: str,
    research_id: str,
    status: str,
    started_at: str | None,
    completed_at: str | None,
    attempt_started_at: str | None = None,
    deadline_at: str | None = None,
    idempotent_readback: bool,
    result_ready: bool,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    status_url, result_url = _operation_urls(request_id)
    return {
        "schema_version": OPERATION_SCHEMA_VERSION,
        "operation": "mastery_research_start",
        "request_id": request_id,
        "request_fingerprint": fingerprint,
        "research_id": research_id,
        "status": status,
        "result_ready": result_ready,
        "idempotent_readback": idempotent_readback,
        "status_url": status_url,
        "result_url": result_url,
        "retry_after_seconds": (
            5 if status == "running" else 10 if status in {"queued", "saturated"} else None
        ),
        "error_code": error_code,
        "error_message": error_message,
        "started_at": started_at,
        "operation_started_at": started_at,
        "attempt_started_at": attempt_started_at,
        "deadline_at": deadline_at,
        "completed_at": completed_at,
    }


def _start_receipt_from_existing(
    request: AutomationResearchRequest,
    fingerprint: str,
    result: AutomationHTTPResult,
) -> AutomationHTTPResult:
    body = result.body
    status = str(body.get("status") or "failed")
    completed_at = body.get("completed_at")
    result_ready = (
        status in {"completed", "failed"}
        and bool(completed_at)
        and body.get("error_code") != "idempotency_conflict"
    )
    return AutomationHTTPResult(
        status_code=result.status_code,
        body=_operation_receipt(
            request_id=request.request_id,
            fingerprint=fingerprint,
            research_id=str(body.get("research_id") or ""),
            status=status,
            started_at=body.get("started_at"),
            attempt_started_at=body.get("attempt_started_at"),
            deadline_at=body.get("deadline_at"),
            completed_at=completed_at,
            idempotent_readback=bool(body.get("idempotent_readback")),
            result_ready=result_ready,
            error_code=body.get("error_code"),
            error_message=body.get("error_message"),
        ),
        headers=result.headers,
    )


def _track_background_task(
    request: AutomationResearchRequest,
    store: AutomationResearchStore,
    claim: AutomationExecutionClaim,
) -> asyncio.Task[AutomationHTTPResult]:
    key = _background_task_key(store, request.request_id)
    existing = _background_tasks.get(key)
    if existing is not None and not existing.done():
        raise RuntimeError("An active task already owns this automation request")
    store_key = key[0]
    active_for_store = sum(
        1
        for (path, _), active in _background_tasks.items()
        if path == store_key and not active.done()
    )
    if active_for_store >= _automation_capacity().max_concurrent:
        raise RuntimeError("Automation task capacity invariant was exceeded")
    task = asyncio.create_task(
        _execute_automation_claim(
            request,
            store,
            claim,
            persist_cancellation=False,
        ),
        name=f"mastery-research:{request.request_id}",
    )
    _background_tasks[key] = task

    def cleanup(completed: asyncio.Task[AutomationHTTPResult]) -> None:
        if _background_tasks.get(key) is completed:
            _background_tasks.pop(key, None)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:  # pragma: no cover - executor normally persists errors
            logger.error(
                "automation background task crashed for request_id=%s",
                request.request_id,
                exc_info=(type(error), error, error.__traceback__),
            )
        _schedule_queue_drain(store)

    task.add_done_callback(cleanup)
    return task


async def _drain_queued_jobs(store: AutomationResearchStore) -> None:
    """Recover stale attempts and promote FIFO work within durable capacity."""

    capacity = _automation_capacity()
    store_key = str(store.path.resolve())
    while True:
        active_for_store = sum(
            1
            for (path, _), task in _background_tasks.items()
            if path == store_key and not task.done()
        )
        if active_for_store >= capacity.max_concurrent:
            return
        promoted = await asyncio.to_thread(
            store.claim_next_stale,
            stale_before=_stale_cutoff_iso(),
        )
        recovered_stale = promoted is not None
        if promoted is None:
            if not await asyncio.to_thread(store.has_queued):
                if not await asyncio.to_thread(store.has_running):
                    return
                # Keep one bounded supervisor alive after boot so a lone
                # crashed receipt is reclaimed when it *later* crosses the
                # stale boundary; pure GET polling remains mutation-free.
                await asyncio.sleep(_queue_recovery_poll_seconds())
                continue
            promoted = await asyncio.to_thread(
                store.claim_next_queued,
                capacity=capacity,
            )
        if promoted is None:
            await asyncio.sleep(_queue_recovery_poll_seconds())
            continue
        payload = promoted.get("request")
        try:
            request = AutomationResearchRequest.model_validate(payload)
        except ValidationError:
            logger.error(
                "queued automation receipt has invalid persisted request_id=%s",
                promoted.get("request_id"),
            )
            completed_at = utc_now_iso()
            corrupt_response = {
                "schema_version": RESULT_SCHEMA_VERSION,
                "request_id": promoted["request_id"],
                "request_fingerprint": promoted["request_fingerprint"],
                "research_id": promoted["research_id"],
                "core_run_id": promoted.get("core_run_id")
                or promoted["research_id"],
                "lease_generation": int(promoted.get("lease_generation") or 1),
                "status": "failed",
                "publishable": False,
                "idempotent_readback": False,
                "error_code": "persisted_request_invalid",
                "error_message": (
                    "Durable queued request no longer matches the versioned input contract"
                ),
                "report": None,
                "report_path": None,
                "source_count": 0,
                "source_urls": [],
                "source_manifest": None,
                "report_quality": None,
                "image_count": 0,
                "images": [],
                "cost_usd": 0.0,
                "delivery": {"attempted": False},
                "started_at": promoted["started_at"],
                "attempt_started_at": promoted.get("attempt_started_at")
                or promoted["started_at"],
                "deadline_at": promoted.get("deadline_at"),
                "completed_at": completed_at,
            }
            snapshot_manifest, snapshot_id = _snapshot_manifest(
                fingerprint=str(promoted["request_fingerprint"]),
                research_id=str(promoted["research_id"]),
                core_run_id=str(
                    promoted.get("core_run_id") or promoted["research_id"]
                ),
                lease_generation=int(promoted.get("lease_generation") or 1),
                attempt_started_at=str(
                    promoted.get("attempt_started_at") or promoted["started_at"]
                ),
                deadline_at=promoted.get("deadline_at"),
                response=corrupt_response,
            )
            corrupt_response["source_snapshot_manifest"] = snapshot_manifest
            corrupt_response["source_snapshot_id"] = snapshot_id
            corrupt_response["contract_manifest"] = json.loads(
                _json_dump(ASYNC_CONTRACT_MANIFEST)
            )
            try:
                await asyncio.to_thread(
                    store.finish,
                    promoted["request_id"],
                    promoted["request_fingerprint"],
                    promoted["lease_token"],
                    corrupt_response,
                )
            except AutomationLeaseLost:
                pass
            continue
        prior_core_run: dict[str, Any] | None = None
        resume_report_only = False
        if recovered_stale:
            prior_core_id = promoted.get("prior_core_run_id")
            if isinstance(prior_core_id, str):
                prior_core_run = ResearchRunStore(
                    store.path, recover_interrupted=False
                ).get_run(prior_core_id)
                if prior_core_run is not None:
                    reconciled = _terminal_response_from_core_run(
                        request,
                        str(promoted["request_fingerprint"]),
                        promoted,
                        prior_core_run,
                    )
                    if reconciled is not None:
                        adopted = await asyncio.to_thread(
                            store.adopt_terminal_core_run,
                            promoted["request_id"],
                            promoted["request_fingerprint"],
                            promoted["lease_token"],
                            prior_core_id,
                        )
                        if adopted:
                            promoted["core_run_id"] = prior_core_id
                            try:
                                await asyncio.to_thread(
                                    store.finish,
                                    promoted["request_id"],
                                    promoted["request_fingerprint"],
                                    promoted["lease_token"],
                                    reconciled,
                                )
                            except AutomationLeaseLost:
                                pass
                            continue
                resume_report_only = bool(
                    prior_core_run
                    and prior_core_run.get("status") == "completed"
                    and (prior_core_run.get("source_manifest") or {}).get("status")
                    == "passed"
                    and not prior_core_run.get("report_quality")
                )
                if not resume_report_only:
                    prior_core_run = None
        _track_background_task(
            request,
            store,
            AutomationExecutionClaim(
                fingerprint=str(promoted["request_fingerprint"]),
                reservation=promoted,
                core_run=prior_core_run,
                resume_report_only=resume_report_only,
                idempotent_readback=True,
            ),
        )


def _schedule_queue_drain(store: AutomationResearchStore) -> None:
    """Keep at most one lightweight queue scheduler per durable store."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    store_key = str(store.path.resolve())
    current = _queue_drainers.get(store_key)
    if current is not None and not current.done():
        return
    task = loop.create_task(
        _drain_queued_jobs(store),
        name=f"mastery-research-queue:{store.path.name}",
    )
    _queue_drainers[store_key] = task

    def cleanup(completed: asyncio.Task[None]) -> None:
        if _queue_drainers.get(store_key) is completed:
            _queue_drainers.pop(store_key, None)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "automation queue drain failed for store=%s",
                store.path,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(cleanup)


def schedule_automation_research_recovery() -> None:
    """Schedule boot-time recovery without making status/result mutate state."""

    try:
        store = get_automation_research_store()
    except (OSError, sqlite3.Error) as exc:
        logger.error("automation boot recovery store is unavailable: %s", exc)
        return
    _schedule_queue_drain(store)


async def start_automation_research(
    request: AutomationResearchRequest,
    *,
    store: AutomationResearchStore | None = None,
) -> AutomationHTTPResult:
    """Reserve one durable operation and return before research work begins."""

    store = store or get_automation_research_store()
    fingerprint = request_fingerprint(request)
    claim = await _claim_automation_execution(
        request,
        store,
        bounded_admission=True,
    )
    if isinstance(claim, AutomationHTTPResult):
        _schedule_queue_drain(store)
        return _start_receipt_from_existing(request, fingerprint, claim)

    _track_background_task(request, store, claim)
    reservation = claim.reservation
    return AutomationHTTPResult(
        status_code=202,
        body=_operation_receipt(
            request_id=request.request_id,
            fingerprint=fingerprint,
            research_id=reservation["research_id"],
            status="running",
            started_at=reservation["started_at"],
            attempt_started_at=reservation.get("attempt_started_at")
            or reservation["started_at"],
            deadline_at=reservation.get("deadline_at"),
            completed_at=None,
            idempotent_readback=claim.idempotent_readback,
            result_ready=False,
        ),
        headers={"Retry-After": "5"},
    )


def _lookup_error(
    request_id: str,
    *,
    operation: str,
    schema_version: str,
) -> AutomationHTTPResult:
    body = {
        "schema_version": schema_version,
        "operation": operation,
        "request_id": request_id,
        "status": "not_found",
        "error_code": "automation_request_not_found",
        "error_message": "No durable Mastery Research operation has this request_id",
    }
    if operation == "mastery_research_result":
        body["contract_manifest"] = ASYNC_CONTRACT_MANIFEST
    return AutomationHTTPResult(
        status_code=404,
        body=body,
    )


def _receipt_read_failure(
    request_id: str,
    *,
    operation: str,
    schema_version: str,
    error: AutomationReceiptReadError,
) -> AutomationHTTPResult:
    body = {
        "schema_version": schema_version,
        "operation": operation,
        "request_id": request_id,
        "status": "failed",
        "error_code": error.code,
        "error_message": error.message,
    }
    if operation == "mastery_research_result":
        body["contract_manifest"] = ASYNC_CONTRACT_MANIFEST
    return AutomationHTTPResult(
        status_code=error.status_code,
        body=body,
        headers={"Retry-After": "10"} if error.status_code == 503 else {},
    )


def _status_readback(existing: dict[str, Any]) -> dict[str, Any]:
    response = existing.get("response")
    status = str((response or {}).get("status") or existing.get("status") or "failed")
    status_url, result_url = _operation_urls(existing["request_id"])
    result_ready = response is not None and status in {"completed", "failed"}
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "operation": "mastery_research_status",
        "request_id": existing["request_id"],
        "request_fingerprint": existing["request_fingerprint"],
        "research_id": existing["research_id"],
        "core_run_id": (
            None
            if status == "queued"
            else existing.get("core_run_id") or existing["research_id"]
        ),
        "lease_generation": int(existing.get("lease_generation") or 1),
        "status": status,
        "result_ready": result_ready,
        "publishable": bool((response or {}).get("publishable")),
        "status_url": status_url,
        "result_url": result_url,
        "retry_after_seconds": (
            5 if status == "running" else 10 if status == "queued" else None
        ),
        "error_code": (response or {}).get("error_code") or existing.get("error_code"),
        "error_message": (response or {}).get("error_message")
        or existing.get("error_message"),
        "started_at": existing.get("started_at"),
        "operation_started_at": existing.get("started_at"),
        "attempt_started_at": existing.get("attempt_started_at")
        or existing.get("started_at"),
        "deadline_at": existing.get("deadline_at"),
        "completed_at": (response or {}).get("completed_at")
        or existing.get("completed_at"),
    }


def _read_receipt_without_initializing(request_id: str) -> dict[str, Any] | None:
    """Read the durable ledger through SQLite read-only mode.

    Status/result endpoints use this on a cold process so observing a job never
    creates a database, migrates a table, rotates a lease, or updates a row.
    """

    path = get_research_run_store_path()
    if not path.is_file():
        raise AutomationReceiptReadError(
            "automation_store_unavailable",
            "Mastery Research receipt store is not available on this service instance",
            503,
        )
    uri_path = quote(str(path.resolve()), safe="/")
    try:
        with sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM automation_research_requests
                 WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        logger.warning("automation receipt store unavailable during pure read: %s", exc)
        raise AutomationReceiptReadError(
            "automation_store_unavailable",
            "Mastery Research receipt store could not be read on this service instance",
            503,
        ) from exc
    except sqlite3.DatabaseError as exc:
        logger.error("automation receipt store is corrupt during pure read: %s", exc)
        raise AutomationReceiptReadError(
            "automation_store_corrupt",
            "Mastery Research receipt store is not a valid readable SQLite database",
            500,
        ) from exc
    try:
        return AutomationResearchStore._row(row)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AutomationReceiptReadError(
            "idempotency_receipt_corrupt",
            "Durable Mastery Research receipt contains invalid persisted JSON",
            500,
        ) from exc


def _validate_persisted_receipt(existing: dict[str, Any]) -> None:
    """Reject structurally misleading durable rows before any readback."""

    required = ("request_id", "request_fingerprint", "research_id", "status")
    if any(not isinstance(existing.get(key), str) or not existing[key] for key in required):
        raise ValueError("Durable receipt identity fields are missing")
    if existing["status"] not in {"queued", "running", "completed", "failed"}:
        raise ValueError("Durable receipt status is invalid")
    receipt_schema_version = existing.get("receipt_schema_version")
    if receipt_schema_version not in {
        DURABLE_RECEIPT_SCHEMA_VERSION,
        LEGACY_DURABLE_RECEIPT_SCHEMA_VERSION,
    }:
        raise ValueError("Durable receipt schema version is missing or invalid")
    request_payload = existing.get("request")
    if request_payload is not None:
        request = AutomationResearchRequest.model_validate(request_payload)
        if request.request_id != existing["request_id"]:
            raise ValueError("Persisted request_id does not match its receipt")
        if request_fingerprint(request) != existing["request_fingerprint"]:
            raise ValueError("Persisted request fingerprint does not match its receipt")
    response = existing.get("response")
    if response is None:
        if existing["status"] not in {"queued", "running"}:
            raise ValueError("Terminal durable receipt is missing its response")
        return
    if existing["status"] not in {"completed", "failed"}:
        raise ValueError("Nonterminal durable receipt unexpectedly contains a response")
    for key in ("request_id", "request_fingerprint", "research_id"):
        if response.get(key) != existing[key]:
            raise ValueError(f"Persisted response {key} does not match its receipt")
    if response.get("status") != existing["status"]:
        raise ValueError("Persisted response status does not match its receipt")
    if not isinstance(response.get("completed_at"), str) or not response["completed_at"]:
        raise ValueError("Terminal persisted response is missing completed_at")
    if existing["status"] == "completed":
        if response.get("publishable") is not True:
            raise ValueError("Completed persisted response is not publishable")
        if not isinstance(response.get("report"), str) or not response["report"].strip():
            raise ValueError("Completed persisted response is missing its report")
    elif not isinstance(response.get("error_code"), str) or not response["error_code"]:
        raise ValueError("Failed persisted response is missing its error code")
    contract = response.get("contract_manifest")
    snapshot = response.get("source_snapshot_manifest")
    snapshot_id = response.get("source_snapshot_id")
    if contract is None and snapshot is None and snapshot_id is None:
        if receipt_schema_version == LEGACY_DURABLE_RECEIPT_SCHEMA_VERSION:
            return
        raise ValueError("Current durable terminal receipt is missing frozen lineage")
    if not isinstance(contract, dict) or not isinstance(snapshot, dict):
        raise ValueError("Frozen terminal lineage manifests are incomplete")
    descriptor = contract.get("descriptor")
    if not isinstance(descriptor, dict):
        raise ValueError("Frozen contract descriptor is invalid")
    expected_contract_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(
            descriptor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if contract.get("contractFingerprint") != expected_contract_fingerprint:
        raise ValueError("Frozen contract fingerprint does not match its descriptor")
    expected_core_run_id = response.get("core_run_id") or existing.get("core_run_id")
    if snapshot.get("request_fingerprint") != _sha256_ref(
        existing["request_fingerprint"]
    ):
        raise ValueError("Source snapshot request fingerprint is invalid")
    if snapshot.get("research_id") != existing["research_id"]:
        raise ValueError("Source snapshot research identity is invalid")
    if snapshot.get("core_run_id") != expected_core_run_id:
        raise ValueError("Source snapshot core-run identity is invalid")
    if int(snapshot.get("lease_generation") or 0) != int(
        response.get("lease_generation") or existing.get("lease_generation") or 1
    ):
        raise ValueError("Source snapshot lease generation is invalid")
    report = response.get("report")
    report_text = report if isinstance(report, str) else ""
    report_manifest = snapshot.get("report")
    if not isinstance(report_manifest, dict):
        raise ValueError("Source snapshot report manifest is invalid")
    expected_report_hash = (
        "sha256:" + hashlib.sha256(report_text.encode("utf-8")).hexdigest()
        if report_text
        else None
    )
    if report_manifest != {
        "sha256": expected_report_hash,
        "unicode_codepoints": len(report_text),
        "utf8_bytes": len(report_text.encode("utf-8")),
    }:
        raise ValueError("Source snapshot report metrics do not match the report")
    evidence = {
        "source_count": int(response.get("source_count") or 0),
        "source_urls": response.get("source_urls") or [],
        "source_manifest": response.get("source_manifest"),
        "image_count": int(response.get("image_count") or 0),
        "images": response.get("images") or [],
    }
    expected_evidence_fingerprint = "sha256:" + hashlib.sha256(
        _json_dump(evidence).encode("utf-8")
    ).hexdigest()
    if snapshot.get("evidence_fingerprint") != expected_evidence_fingerprint:
        raise ValueError("Source snapshot evidence fingerprint is invalid")
    expected_snapshot_id = (
        f"mastery-research:{existing['research_id']}@sha256:"
        + hashlib.sha256(_json_dump(snapshot).encode("utf-8")).hexdigest()
    )
    if snapshot_id != expected_snapshot_id:
        raise ValueError("Source snapshot id does not match its frozen manifest")


def _read_receipt(
    request_id: str,
    *,
    store: AutomationResearchStore | None,
) -> dict[str, Any] | None:
    try:
        existing = (
            _read_receipt_without_initializing(request_id)
            if store is None
            else store.get(request_id)
        )
        if existing is not None:
            _validate_persisted_receipt(existing)
        return existing
    except AutomationReceiptReadError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
        raise AutomationReceiptReadError(
            "idempotency_receipt_corrupt",
            "Durable Mastery Research receipt is structurally invalid",
            500,
        ) from exc
    except sqlite3.OperationalError as exc:
        raise AutomationReceiptReadError(
            "automation_store_unavailable",
            "Mastery Research receipt store could not be read on this service instance",
            503,
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise AutomationReceiptReadError(
            "automation_store_corrupt",
            "Mastery Research receipt store is not a valid readable SQLite database",
            500,
        ) from exc


def read_automation_research_status(
    request_id: str,
    *,
    store: AutomationResearchStore | None = None,
) -> AutomationHTTPResult:
    """Read operation state without reconciliation, reclaiming, or heartbeats."""

    try:
        existing = _read_receipt(request_id, store=store)
    except AutomationReceiptReadError as exc:
        return _receipt_read_failure(
            request_id,
            operation="mastery_research_status",
            schema_version=STATUS_SCHEMA_VERSION,
            error=exc,
        )
    if existing is None:
        return _lookup_error(
            request_id,
            operation="mastery_research_status",
            schema_version=STATUS_SCHEMA_VERSION,
        )
    body = _status_readback(existing)
    return AutomationHTTPResult(
        status_code=200,
        body=body,
        headers=(
            {"Retry-After": "5"}
            if body["status"] == "running"
            else {"Retry-After": "10"}
            if body["status"] == "queued"
            else {}
        ),
    )


def _sha256_ref(fingerprint: str) -> str:
    return fingerprint if fingerprint.startswith("sha256:") else f"sha256:{fingerprint}"


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _bounded_k2_text(value: str, limit: int) -> str:
    """Bound strings by JavaScript/Zod UTF-16 units, not Python codepoints."""

    if _utf16_units(value) <= limit:
        return value
    remaining = limit - 1  # reserve one UTF-16 unit for the ellipsis
    kept: list[str] = []
    for character in value:
        units = 2 if ord(character) > 0xFFFF else 1
        if units > remaining:
            break
        kept.append(character)
        remaining -= units
    return "".join(kept).rstrip() + "…"


def _source_snapshot_id(
    existing: dict[str, Any], response: dict[str, Any]
) -> str:
    """Identify the exact accepted provider source snapshot handed downstream."""

    persisted = response.get("source_snapshot_id")
    if isinstance(persisted, str) and persisted:
        return persisted
    persisted_manifest = response.get("source_snapshot_manifest")
    if isinstance(persisted_manifest, dict):
        digest = hashlib.sha256(
            _json_dump(persisted_manifest).encode("utf-8")
        ).hexdigest()
        return f"mastery-research:{existing['research_id']}@sha256:{digest}"
    # Compatibility for receipts completed before source snapshots were frozen.
    report = response.get("report")
    descriptor = {
        "research_id": existing["research_id"],
        "core_run_id": response.get("core_run_id")
        or existing.get("core_run_id")
        or existing["research_id"],
        "report_sha256": (
            hashlib.sha256(report.encode("utf-8")).hexdigest()
            if isinstance(report, str)
            else None
        ),
        "source_urls": response.get("source_urls") or [],
        "source_manifest": response.get("source_manifest"),
    }
    digest = hashlib.sha256(_json_dump(descriptor).encode("utf-8")).hexdigest()
    return f"mastery-research:{existing['research_id']}@sha256:{digest}"


def _frozen_contract_manifest(response: dict[str, Any]) -> dict[str, Any]:
    persisted = response.get("contract_manifest")
    if isinstance(persisted, dict):
        return persisted
    fallback = json.loads(_json_dump(ASYNC_CONTRACT_MANIFEST))
    fallback["lineageBasis"] = "legacy_current_derived"
    return fallback


def _upstream_execution_receipt(
    existing: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schemaVersion": "upstream_execution_receipt.v1",
        "provider": "mastery-research",
        "capabilityRef": "tool:mastery-research",
        "operation": "mastery_research_result",
        "operationTitle": "Mastery Research result",
        "contractFingerprint": _frozen_contract_manifest(response)[
            "contractFingerprint"
        ],
        "requestFingerprint": _sha256_ref(existing["request_fingerprint"]),
        "sourceSnapshotId": _source_snapshot_id(existing, response),
        "auditId": f"mastery-research:{existing['request_id']}",
        "runId": response.get("core_run_id")
        or existing.get("core_run_id")
        or existing["research_id"],
        "status": "succeeded" if response.get("status") == "completed" else "failed",
        "startedAt": response.get("attempt_started_at")
        or existing.get("attempt_started_at")
        or existing.get("started_at"),
        "completedAt": response.get("completed_at") or existing.get("completed_at"),
    }


def _k2_handoff(
    existing: dict[str, Any],
    response: dict[str, Any],
    upstream_execution: dict[str, Any],
) -> dict[str, Any] | None:
    report = response.get("report")
    if (
        response.get("status") != "completed"
        or response.get("publishable") is not True
        or not isinstance(report, str)
        or not report.strip()
    ):
        return None

    stored_request = existing.get("request") or {}
    query = str(stored_request.get("query") or existing["research_id"])
    report_prompt = str(stored_request.get("report_prompt") or "")
    mission = (
        "Treat this accepted, source-backed Mastery Research report as derived "
        "secondary context. Extract its most durable, actionable, distinctive "
        "insights with enough context to guide future work; verify useful patterns "
        "independently and avoid time-bound details that will rot."
    )
    if report_prompt:
        mission += (
            "\n\nBounded research-brief excerpt for context only; the complete "
            "normalized brief remains in the result provenance:\n"
            f"{_bounded_k2_text(report_prompt, 2_800)}"
        )
    source_urls = list(
        dict.fromkeys(
            url
            for url in response.get("source_urls") or []
            if isinstance(url, str) and url.strip()
        )
    )[:50]
    completed_at = response.get("completed_at") or existing.get("completed_at")
    report_chars = len(report)
    report_bytes = len(report.encode("utf-8"))
    if report_chars > K2_INLINE_MAX_CHARS or report_bytes > K2_INLINE_MAX_BYTES:
        return {
            "schema_version": K2_HANDOFF_SCHEMA_VERSION,
            "status": "withheld",
            "tool": "knowledge.refine.preview",
            "reason": {
                "code": "k2_inline_content_limit_exceeded",
                "message": (
                    "Accepted report exceeds the downstream inline character or UTF-8 byte limit"
                ),
            },
            "content_metrics": {
                "unicode_codepoints": report_chars,
                "utf8_bytes": report_bytes,
                "max_characters": K2_INLINE_MAX_CHARS,
                "max_utf8_bytes": K2_INLINE_MAX_BYTES,
            },
            "locator": f"research-run:{existing['research_id']}",
            "report_path": response.get("report_path"),
            "source_snapshot_id": upstream_execution["sourceSnapshotId"],
            "upstream_execution": upstream_execution,
        }
    return {
        "schema_version": K2_HANDOFF_SCHEMA_VERSION,
        "status": "ready",
        "tool": "knowledge.refine.preview",
        "arguments": {
            "content": report,
            "title": _bounded_k2_text(f"Mastery Research — {query}", 1_000),
            "mission": _bounded_k2_text(mission, 4_000),
            "transport": "automation",
            "sourceId": f"mastery-research:{existing['research_id']}",
            "locator": f"research-run:{existing['research_id']}",
            "provenanceType": "derived_synthesis",
            "capturedAt": completed_at,
            "sourceLinks": source_urls,
            "upstreamExecution": upstream_execution,
            "researchDepth": "verify",
            "maxResearchSources": 8,
            "requestId": f"refinery:{existing['research_id']}",
            "idempotencyKey": f"mastery-research:{existing['research_id']}",
        },
    }


def _terminal_result_readback(existing: dict[str, Any]) -> dict[str, Any]:
    response = existing["response"]
    stored_request = existing.get("request") or {}
    upstream_execution = _upstream_execution_receipt(existing, response)
    source_snapshot_id = upstream_execution["sourceSnapshotId"]
    normalized_request = {
        key: stored_request.get(key)
        for key in (
            "schema_version",
            "request_id",
            "query",
            "report_prompt",
            "report_type",
            "report_source",
            "tone",
            "scope",
            "depth",
            "max_sources_per_query",
            "include_generated_images",
            "source_policy",
        )
        if key in stored_request
    }
    provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "provider": "mastery-research",
        "capability_ref": "tool:mastery-research",
        "contract_fingerprint": _frozen_contract_manifest(response)[
            "contractFingerprint"
        ],
        "request_fingerprint": _sha256_ref(existing["request_fingerprint"]),
        "request_schema_version": stored_request.get(
            "schema_version", REQUEST_SCHEMA_VERSION
        ),
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "request_id": existing["request_id"],
        "research_id": existing["research_id"],
        "core_run_id": response.get("core_run_id")
        or existing.get("core_run_id")
        or existing["research_id"],
        "lease_generation": int(
            response.get("lease_generation")
            or existing.get("lease_generation")
            or 1
        ),
        "source_snapshot_id": source_snapshot_id,
        "query": stored_request.get("query"),
        "report_prompt": stored_request.get("report_prompt"),
        "source_policy": stored_request.get("source_policy"),
        "started_at": existing.get("started_at"),
        "operation_started_at": existing.get("started_at"),
        "attempt_started_at": response.get("attempt_started_at")
        or existing.get("attempt_started_at")
        or existing.get("started_at"),
        "deadline_at": response.get("deadline_at") or existing.get("deadline_at"),
        "completed_at": response.get("completed_at") or existing.get("completed_at"),
    }
    return {
        "schema_version": RESULT_READ_SCHEMA_VERSION,
        "operation": "mastery_research_result",
        "request_id": existing["request_id"],
        "request_fingerprint": existing["request_fingerprint"],
        "research_id": existing["research_id"],
        "core_run_id": response.get("core_run_id")
        or existing.get("core_run_id")
        or existing["research_id"],
        "status": response.get("status"),
        "publishable": bool(response.get("publishable")),
        "idempotent_readback": True,
        "error_code": response.get("error_code"),
        "error_message": response.get("error_message"),
        "normalized_request": normalized_request,
        "report": response.get("report"),
        "report_path": response.get("report_path"),
        "evidence": {
            "source_count": int(response.get("source_count") or 0),
            "source_urls": list(response.get("source_urls") or []),
            "source_manifest": response.get("source_manifest"),
            "image_count": int(response.get("image_count") or 0),
            "images": list(response.get("images") or []),
        },
        "quality": response.get("report_quality"),
        "cost": {
            "amount": float(response.get("cost_usd") or 0.0),
            "currency": "USD",
        },
        "provenance": provenance,
        "source_snapshot_id": source_snapshot_id,
        "contract_manifest": _frozen_contract_manifest(response),
        "source_snapshot_manifest": response.get("source_snapshot_manifest")
        or {
            "schema_version": "mastery_research_source_snapshot.v1",
            "lineage_basis": "legacy_current_derived",
        },
        "upstream_execution": upstream_execution,
        "k2_handoff": _k2_handoff(existing, response, upstream_execution),
        "delivery": response.get("delivery") or {"attempted": False},
        "started_at": existing.get("started_at"),
        "operation_started_at": existing.get("started_at"),
        "attempt_started_at": response.get("attempt_started_at")
        or existing.get("attempt_started_at")
        or existing.get("started_at"),
        "deadline_at": response.get("deadline_at") or existing.get("deadline_at"),
        "completed_at": response.get("completed_at") or existing.get("completed_at"),
    }


def read_automation_research_result(
    request_id: str,
    *,
    store: AutomationResearchStore | None = None,
) -> AutomationHTTPResult:
    """Read a terminal result without reconciling or changing operation state."""

    try:
        existing = _read_receipt(request_id, store=store)
    except AutomationReceiptReadError as exc:
        return _receipt_read_failure(
            request_id,
            operation="mastery_research_result",
            schema_version=RESULT_READ_SCHEMA_VERSION,
            error=exc,
        )
    if existing is None:
        return _lookup_error(
            request_id,
            operation="mastery_research_result",
            schema_version=RESULT_READ_SCHEMA_VERSION,
        )
    if existing.get("response") is None:
        if existing.get("status") not in {"queued", "running"}:
            return AutomationHTTPResult(
                status_code=500,
                body={
                    "schema_version": RESULT_READ_SCHEMA_VERSION,
                    "operation": "mastery_research_result",
                    "request_id": request_id,
                    "research_id": existing["research_id"],
                    "status": "failed",
                    "error_code": "idempotency_receipt_corrupt",
                    "error_message": "Terminal operation is missing its stored result",
                    "contract_manifest": ASYNC_CONTRACT_MANIFEST,
                },
            )
        status = _status_readback(existing)
        operation_status = str(existing.get("status"))
        retry_after = "10" if operation_status == "queued" else "5"
        return AutomationHTTPResult(
            status_code=202,
            body={
                "schema_version": RESULT_READ_SCHEMA_VERSION,
                "operation": "mastery_research_result",
                "request_id": request_id,
                "request_fingerprint": existing["request_fingerprint"],
                "research_id": existing["research_id"],
                "status": operation_status,
                "result_ready": False,
                "status_url": status["status_url"],
                "result_url": status["result_url"],
                "retry_after_seconds": int(retry_after),
                "started_at": existing.get("started_at"),
                "operation_started_at": existing.get("started_at"),
                "attempt_started_at": existing.get("attempt_started_at")
                or existing.get("started_at"),
                "deadline_at": existing.get("deadline_at"),
                "completed_at": None,
                "contract_manifest": ASYNC_CONTRACT_MANIFEST,
            },
            headers={"Retry-After": retry_after},
        )
    return AutomationHTTPResult(
        status_code=200,
        body=_terminal_result_readback(existing),
    )


def _validation_failure(exc: ValidationError) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": "failed",
        "publishable": False,
        "idempotent_readback": False,
        "error_code": "invalid_request",
        "error_message": "Request body does not match research_automation_request.v1",
        "details": json.loads(exc.json(include_url=False)),
        "delivery": {"attempted": False},
    }


async def automation_research_route(request: Request) -> JSONResponse:
    """Handle the legacy blocking research facade; auth is outer middleware."""

    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content={
                "schema_version": RESULT_SCHEMA_VERSION,
                "status": "failed",
                "publishable": False,
                "idempotent_readback": False,
                "error_code": "invalid_json",
                "error_message": "Request body must be valid JSON",
                "delivery": {"attempted": False},
            },
        )
    try:
        parsed = AutomationResearchRequest.model_validate(payload)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content=_validation_failure(exc))
    result = await execute_automation_research(parsed)
    return JSONResponse(
        status_code=result.status_code,
        content=result.body,
        headers=result.headers,
    )


async def automation_research_start_route(request: Request) -> JSONResponse:
    """Start durable research work and return its operation receipt immediately."""

    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content={
                "schema_version": OPERATION_SCHEMA_VERSION,
                "operation": "mastery_research_start",
                "status": "failed",
                "error_code": "invalid_json",
                "error_message": "Request body must be valid JSON",
            },
        )
    try:
        parsed = AutomationResearchRequest.model_validate(payload)
    except ValidationError as exc:
        failure = _validation_failure(exc)
        failure.update(
            schema_version=OPERATION_SCHEMA_VERSION,
            operation="mastery_research_start",
        )
        return JSONResponse(status_code=422, content=failure)
    result = await start_automation_research(parsed)
    return JSONResponse(
        status_code=result.status_code,
        content=result.body,
        headers=result.headers,
    )


def _path_request_id(request: Request) -> str | None:
    request_id = str(request.path_params.get("request_id") or "")
    return request_id if _REQUEST_ID_PATTERN.fullmatch(request_id) else None


def _invalid_path_response(operation: str) -> JSONResponse:
    schema_version = (
        RESULT_READ_SCHEMA_VERSION
        if operation == "mastery_research_result"
        else STATUS_SCHEMA_VERSION
    )
    content: dict[str, Any] = {
        "schema_version": schema_version,
        "operation": operation,
        "status": "failed",
        "error_code": "invalid_request_id",
        "error_message": "request_id path value is invalid",
    }
    if operation == "mastery_research_result":
        content["contract_manifest"] = ASYNC_CONTRACT_MANIFEST
    return JSONResponse(
        status_code=422,
        content=content,
    )


async def automation_research_status_route(request: Request) -> JSONResponse:
    """Return a pure status readback; auth is outer middleware."""

    request_id = _path_request_id(request)
    if request_id is None:
        return _invalid_path_response("mastery_research_status")
    result = read_automation_research_status(request_id)
    return JSONResponse(
        status_code=result.status_code,
        content=result.body,
        headers=result.headers,
    )


async def automation_research_result_route(request: Request) -> JSONResponse:
    """Return a pure terminal-result readback; auth is outer middleware."""

    request_id = _path_request_id(request)
    if request_id is None:
        return _invalid_path_response("mastery_research_result")
    result = read_automation_research_result(request_id)
    return JSONResponse(
        status_code=result.status_code,
        content=result.body,
        headers=result.headers,
    )


def install_automation_research_route(mcp: FastMCP) -> None:
    """Install blocking compatibility plus nonblocking operation routes."""

    mcp.custom_route("/automation/research/v1", methods=["POST"])(
        automation_research_route
    )
    mcp.custom_route(f"{ASYNC_JOB_BASE_PATH}/start", methods=["POST"])(
        automation_research_start_route
    )
    mcp.custom_route(
        f"{ASYNC_JOB_BASE_PATH}/{{request_id}}/status", methods=["GET"]
    )(automation_research_status_route)
    mcp.custom_route(
        f"{ASYNC_JOB_BASE_PATH}/{{request_id}}/result", methods=["GET"]
    )(automation_research_result_route)
