"""Authenticated, idempotent HTTP facade for HLT research automations.

The hosted MCP service remains the research authority.  This module adds one
small JSON route for clients such as Make.com that cannot conveniently drive a
stateful MCP session.  It composes the existing strict ``deep_research`` and
``write_report`` functions and deliberately has no delivery integrations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from starlette.requests import Request
from starlette.responses import JSONResponse

from gpt_researcher.research_run_store import (
    INTERRUPTED_ERROR_CODE,
    ResearchRunStore,
    get_research_run_store_path,
    utc_now_iso,
)
from gpt_researcher.source_policy import (
    SourcePolicy,
    SourcePolicyError,
    canonicalize_url,
)
from mcp_server.tools import SourcePolicyInput, deep_research_tool, write_report_tool

logger = logging.getLogger(__name__)

REQUEST_SCHEMA_VERSION = "research_automation_request.v1"
RESULT_SCHEMA_VERSION = "research_automation_result.v1"
_RESEARCH_ID_NAMESPACE = uuid.UUID("a18f98c2-9a6f-49e8-a654-d826588b7a55")


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())


class StrictSourcePolicyInput(SourcePolicyInput):
    """The strict-only source contract accepted by the automation route."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["source_policy.v1"]
    enforcement: Literal["strict"]

    @model_validator(mode="after")
    def validate_source_policy_semantics(self) -> "StrictSourcePolicyInput":
        try:
            SourcePolicy.from_value(self.model_dump(exclude_none=True))
        except (SourcePolicyError, TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        return self


class AutomationResearchRequest(BaseModel):
    """Versioned request contract for ``POST /automation/research/v1``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["research_automation_request.v1"]
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    query: str = Field(min_length=1, max_length=12_000)
    report_prompt: str = Field(min_length=1, max_length=20_000)
    report_type: Literal["research_report"] = "research_report"
    report_source: Literal["web"] = "web"
    tone: Literal["Objective"] = "Objective"
    scope: Literal["none"] = "none"
    depth: Literal["fast", "balanced", "deep"] = "balanced"
    max_sources_per_query: int | None = Field(default=None, ge=3, le=20)
    include_generated_images: bool = False
    source_policy: StrictSourcePolicyInput

    @field_validator("request_id", "query", "report_prompt", mode="before")
    @classmethod
    def normalize_text_fields(cls, value: Any) -> Any:
        return _normalized_text(value) if isinstance(value, str) else value


@dataclass(frozen=True)
class AutomationHTTPResult:
    """Transport response returned by the adapter executor."""

    status_code: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


def canonical_request(request: AutomationResearchRequest) -> dict[str, Any]:
    """Return normalized semantic fields used for request identity.

    ``request_id`` is the caller's idempotency key, not part of the semantic
    payload.  URL/domain collections are normalized so harmless ordering and
    tracking-parameter differences do not create new work.
    """

    policy = SourcePolicy.from_value(
        request.source_policy.model_dump(exclude_none=True)
    ).to_dict()
    policy["allowed_domains"] = sorted(set(policy["allowed_domains"]))
    policy["denied_domains"] = sorted(set(policy["denied_domains"]))
    policy["required_sources"] = sorted(
        (
            {
                "id": source["id"],
                "family": source["family"],
                "url": canonicalize_url(source["url"]),
            }
            for source in policy["required_sources"]
        ),
        key=lambda source: (source["id"], source["family"], source["url"]),
    )
    return {
        "schema_version": request.schema_version,
        "query": request.query,
        "report_prompt": request.report_prompt,
        "report_type": request.report_type,
        "report_source": request.report_source,
        "tone": request.tone,
        "scope": request.scope,
        "depth": request.depth,
        "max_sources_per_query": request.max_sources_per_query,
        "include_generated_images": request.include_generated_images,
        "source_policy": policy,
    }


def request_fingerprint(request: AutomationResearchRequest) -> str:
    """SHA-256 of the canonical effective request, excluding request_id."""

    encoded = json.dumps(
        canonical_request(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deterministic_research_id(request_id: str, fingerprint: str) -> str:
    """Derive a stable UUID for one caller idempotency key and payload."""

    return str(
        uuid.uuid5(
            _RESEARCH_ID_NAMESPACE,
            f"{REQUEST_SCHEMA_VERSION}\n{request_id}\n{fingerprint}",
        )
    )


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


class AutomationResearchStore:
    """Durable idempotency receipts stored beside the MCP research ledger."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else get_research_run_store_path()
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_research_requests (
                    request_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    research_id TEXT NOT NULL UNIQUE,
                    lease_token TEXT,
                    lease_generation INTEGER NOT NULL DEFAULT 1,
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
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(automation_research_requests)"
                ).fetchall()
            }
            if "lease_token" not in columns:
                connection.execute(
                    "ALTER TABLE automation_research_requests ADD COLUMN lease_token TEXT"
                )
            if "lease_generation" not in columns:
                connection.execute(
                    """
                    ALTER TABLE automation_research_requests
                    ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 1
                    """
                )
            connection.execute(
                """
                UPDATE automation_research_requests
                   SET lease_token = lower(hex(randomblob(16)))
                 WHERE lease_token IS NULL OR lease_token = ''
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_automation_research_status
                    ON automation_research_requests(status)
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["response"] = _json_load(result.pop("response_json", None))
        return result

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM automation_research_requests
                 WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return self._row(row)

    def reserve(
        self, request_id: str, fingerprint: str, research_id: str
    ) -> tuple[dict[str, Any], bool]:
        """Atomically reserve a request ID, returning its canonical row."""

        now = utc_now_iso()
        lease_token = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO automation_research_requests (
                    request_id, request_fingerprint, research_id, lease_token,
                    lease_generation, status, started_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, 'running', ?, ?)
                """,
                (request_id, fingerprint, research_id, lease_token, now, now),
            )
            row = connection.execute(
                """
                SELECT * FROM automation_research_requests
                 WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            connection.commit()
        result = self._row(row)
        if result is None:  # pragma: no cover - SQLite invariant defense
            raise RuntimeError("Automation request reservation was not readable")
        return result, cursor.rowcount == 1

    def finish(
        self,
        request_id: str,
        fingerprint: str,
        lease_token: str,
        response: dict[str, Any],
    ) -> None:
        status = str(response.get("status") or "failed")
        if status not in {"completed", "failed"}:
            raise ValueError("Only terminal automation results can be persisted")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE automation_research_requests
                   SET status = ?, response_json = ?, error_code = ?,
                       error_message = ?, completed_at = ?, updated_at = ?
                 WHERE request_id = ? AND request_fingerprint = ?
                   AND lease_token = ?
                   AND status = 'running'
                   AND response_json IS NULL
                """,
                (
                    status,
                    _json_dump(response),
                    response.get("error_code"),
                    response.get("error_message"),
                    response.get("completed_at"),
                    utc_now_iso(),
                    request_id,
                    fingerprint,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise AutomationLeaseLost(
                    "Automation request lease changed before completion"
                )

    def claim_stale(
        self,
        request_id: str,
        fingerprint: str,
        *,
        expected_lease_token: str,
        stale_before: str,
    ) -> dict[str, Any] | None:
        """Claim one abandoned running receipt without changing its identity."""

        now = utc_now_iso()
        lease_token = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE automation_research_requests
                   SET lease_token = ?,
                       lease_generation = lease_generation + 1,
                       updated_at = ?
                 WHERE request_id = ?
                   AND request_fingerprint = ?
                   AND lease_token = ?
                   AND status = 'running'
                   AND response_json IS NULL
                   AND updated_at <= ?
                """,
                (
                    lease_token,
                    now,
                    request_id,
                    fingerprint,
                    expected_lease_token,
                    stale_before,
                ),
            )
            row = (
                connection.execute(
                    """
                    SELECT * FROM automation_research_requests
                     WHERE request_id = ?
                    """,
                    (request_id,),
                ).fetchone()
                if cursor.rowcount == 1
                else None
            )
            connection.commit()
        return self._row(row)

    def claim_for_reconciliation(
        self,
        request_id: str,
        fingerprint: str,
        *,
        expected_lease_token: str,
    ) -> dict[str, Any] | None:
        """Rotate ownership before converting a terminal core run to a receipt."""

        now = utc_now_iso()
        lease_token = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE automation_research_requests
                   SET lease_token = ?,
                       lease_generation = lease_generation + 1,
                       updated_at = ?
                 WHERE request_id = ?
                   AND request_fingerprint = ?
                   AND lease_token = ?
                   AND status = 'running'
                   AND response_json IS NULL
                """,
                (
                    lease_token,
                    now,
                    request_id,
                    fingerprint,
                    expected_lease_token,
                ),
            )
            row = (
                connection.execute(
                    """
                    SELECT * FROM automation_research_requests
                     WHERE request_id = ?
                    """,
                    (request_id,),
                ).fetchone()
                if cursor.rowcount == 1
                else None
            )
            connection.commit()
        return self._row(row)

    def heartbeat(
        self,
        request_id: str,
        fingerprint: str,
        lease_token: str,
    ) -> bool:
        """Renew the current worker's lease without weakening its fencing."""

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE automation_research_requests
                   SET updated_at = ?
                 WHERE request_id = ?
                   AND request_fingerprint = ?
                   AND lease_token = ?
                   AND status = 'running'
                   AND response_json IS NULL
                """,
                (utc_now_iso(), request_id, fingerprint, lease_token),
            )
        return cursor.rowcount == 1


class AutomationLeaseLost(RuntimeError):
    """The executing worker no longer owns the durable request receipt."""


_store_guard = threading.RLock()
_store: AutomationResearchStore | None = None
_request_locks: dict[str, asyncio.Lock] = {}


def get_automation_research_store() -> AutomationResearchStore:
    global _store
    path = get_research_run_store_path()
    with _store_guard:
        if _store is None or _store.path != path:
            _store = AutomationResearchStore(path)
        return _store


def clear_automation_hot_state() -> None:
    """Clear process-only adapter state; durable request receipts remain."""

    global _store
    _request_locks.clear()
    with _store_guard:
        _store = None


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

    async def renew() -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=_heartbeat_interval_seconds()
                )
                return
            except TimeoutError:
                if not await asyncio.to_thread(
                    store.heartbeat,
                    request_id,
                    fingerprint,
                    lease_token,
                ):
                    return

    task = asyncio.create_task(renew())
    try:
        yield
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
) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "request_id": request.request_id,
        "request_fingerprint": fingerprint,
        "research_id": research_id,
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
        "completed_at": None,
    }


def _terminal_response(
    request: AutomationResearchRequest,
    fingerprint: str,
    research_id: str,
    *,
    started_at: str,
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
        report_result.get("status") == "success"
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
    if existing.get("status") == "running":
        body = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "request_id": request.request_id,
            "request_fingerprint": fingerprint,
            "status": "running",
            "research_id": existing["research_id"],
            "publishable": False,
            "idempotent_readback": True,
            "delivery": {"attempted": False},
            "started_at": existing["started_at"],
            "completed_at": None,
        }
        return AutomationHTTPResult(
            status_code=202,
            body=body,
            headers={"Retry-After": "5"},
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


def _recover_terminal_core_result(
    request: AutomationResearchRequest,
    fingerprint: str,
    existing: dict[str, Any],
    store: AutomationResearchStore,
    run: dict[str, Any] | None = None,
) -> AutomationHTTPResult | None:
    """Close a receipt left running after the strict core reached a terminal state."""

    run = run or ResearchRunStore(store.path, recover_interrupted=False).get_run(
        existing["research_id"]
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


async def execute_automation_research(
    request: AutomationResearchRequest,
    *,
    store: AutomationResearchStore | None = None,
) -> AutomationHTTPResult:
    """Execute or durably read back one exact automation request."""

    store = store or get_automation_research_store()
    fingerprint = request_fingerprint(request)
    research_id = deterministic_research_id(request.request_id, fingerprint)

    # The process-local lock protects only the short ownership transaction.
    # Research itself runs outside it so a retry can immediately observe the
    # durable running row and receive HTTP 202 instead of waiting for minutes.
    lock = _request_lock(request.request_id)
    try:
        async with lock:
            resume_report_only = False
            reservation: dict[str, Any]
            existing = store.get(request.request_id)
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    return _idempotency_conflict(request, fingerprint, existing)
                if existing.get("status") != "running":
                    return _existing_result(request, fingerprint, existing)
                core_run = ResearchRunStore(
                    store.path, recover_interrupted=False
                ).get_run(existing["research_id"])
                if core_run is not None:
                    recovered = _recover_terminal_core_result(
                        request, fingerprint, existing, store, core_run
                    )
                    if recovered is not None:
                        return recovered
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
            else:
                reservation, created = store.reserve(
                    request.request_id,
                    fingerprint,
                    research_id,
                )
                if not created:
                    return _existing_result(request, fingerprint, reservation)
                core_run = None
    finally:
        waiters = getattr(lock, "_waiters", None) or ()
        if (
            _request_locks.get(request.request_id) is lock
            and not lock.locked()
            and not waiters
        ):
            _request_locks.pop(request.request_id, None)

    research_id = reservation["research_id"]
    started_at = reservation["started_at"]
    deep_result: dict[str, Any] | None = None
    report_result: dict[str, Any] | None = None
    async with _lease_heartbeat(
        store,
        request.request_id,
        fingerprint,
        reservation["lease_token"],
    ):
        try:
            policy = request.source_policy.model_dump(exclude_none=True)
            if resume_report_only:
                deep_result = {
                    "status": "success",
                    "source_count": core_run.get("source_count", 0),
                    "source_urls": core_run.get("source_urls") or [],
                    "source_manifest": core_run.get("source_manifest"),
                    "image_count": len(core_run.get("research_images") or []),
                    "images": core_run.get("research_images") or [],
                }
            else:
                deep_result = await deep_research_tool(
                    request.query,
                    request.report_type,
                    request.report_source,
                    request.tone,
                    scope=request.scope,
                    depth=request.depth,
                    max_sources_per_query=request.max_sources_per_query,
                    include_generated_images=request.include_generated_images,
                    source_policy=policy,
                    _research_id=research_id,
                )
            await _renew_lease_or_raise(
                store,
                request.request_id,
                fingerprint,
                reservation["lease_token"],
            )
            if deep_result.get("status") == "success":
                report_result = await write_report_tool(
                    research_id,
                    request.report_prompt,
                )
            await _renew_lease_or_raise(
                store,
                request.request_id,
                fingerprint,
                reservation["lease_token"],
            )
            research_run = ResearchRunStore(
                store.path, recover_interrupted=False
            ).get_run(research_id)
            response = _terminal_response(
                request,
                fingerprint,
                research_id,
                started_at=started_at,
                deep_result=deep_result,
                report_result=report_result,
                research_run=research_run,
            )
        except AutomationLeaseLost:
            latest = store.get(request.request_id)
            return _existing_result(request, fingerprint, latest or reservation)
        except asyncio.CancelledError:
            response = _terminal_response(
                request,
                fingerprint,
                research_id,
                started_at=started_at,
                deep_result=deep_result,
                report_result=report_result,
                research_run=ResearchRunStore(
                    store.path, recover_interrupted=False
                ).get_run(research_id),
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
                deep_result=deep_result,
                report_result=report_result,
                research_run=ResearchRunStore(
                    store.path, recover_interrupted=False
                ).get_run(research_id),
                error_code="adapter_runtime_error",
                error_message=type(exc).__name__,
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
    """Handle the Make-friendly research facade; auth is outer middleware."""

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


def install_automation_research_route(mcp: FastMCP) -> None:
    """Install the isolated HLT automation route on the hosted MCP app."""

    mcp.custom_route("/automation/research/v1", methods=["POST"])(
        automation_research_route
    )
