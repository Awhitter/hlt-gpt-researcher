"""Durable SQLite receipts for the research automation adapter.

The store owns reservation, admission, leases, queue promotion, and the
process-local cache of store instances. Executor tasks and their event-loop
state remain in ``mcp_server.automation_research``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from gpt_researcher.research_run_store import (
    get_research_run_store_path,
    utc_now_iso,
)
from mcp_server import automation_research_contracts as contracts
from mcp_server.automation_research_contracts import (
    AutomationAdmissionSaturated,
    AutomationCapacity,
    AutomationDeadlines,
    DURABLE_RECEIPT_SCHEMA_VERSION,
    LEGACY_DURABLE_RECEIPT_SCHEMA_VERSION,
)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("Durable automation JSON column must contain an object")
    return loaded


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
                    core_run_id TEXT,
                    request_json TEXT,
                    lease_token TEXT,
                    lease_generation INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    response_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    started_at TEXT NOT NULL,
                    attempt_started_at TEXT,
                    deadline_at TEXT,
                    overall_timeout_seconds INTEGER,
                    deep_timeout_seconds INTEGER,
                    report_timeout_seconds INTEGER,
                    receipt_schema_version TEXT NOT NULL DEFAULT 'automation_research_receipt.v2',
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
            if "request_json" not in columns:
                connection.execute(
                    "ALTER TABLE automation_research_requests ADD COLUMN request_json TEXT"
                )
            if "core_run_id" not in columns:
                connection.execute(
                    "ALTER TABLE automation_research_requests ADD COLUMN core_run_id TEXT"
                )
            if "attempt_started_at" not in columns:
                connection.execute(
                    "ALTER TABLE automation_research_requests ADD COLUMN attempt_started_at TEXT"
                )
            if "deadline_at" not in columns:
                connection.execute(
                    "ALTER TABLE automation_research_requests ADD COLUMN deadline_at TEXT"
                )
            if "overall_timeout_seconds" not in columns:
                connection.execute(
                    """
                    ALTER TABLE automation_research_requests
                    ADD COLUMN overall_timeout_seconds INTEGER
                    """
                )
            if "deep_timeout_seconds" not in columns:
                connection.execute(
                    """
                    ALTER TABLE automation_research_requests
                    ADD COLUMN deep_timeout_seconds INTEGER
                    """
                )
            if "report_timeout_seconds" not in columns:
                connection.execute(
                    """
                    ALTER TABLE automation_research_requests
                    ADD COLUMN report_timeout_seconds INTEGER
                    """
                )
            if "receipt_schema_version" not in columns:
                connection.execute(
                    """
                    ALTER TABLE automation_research_requests
                    ADD COLUMN receipt_schema_version TEXT
                    """
                )
                connection.execute(
                    """
                    UPDATE automation_research_requests
                       SET receipt_schema_version = ?
                     WHERE receipt_schema_version IS NULL
                    """,
                    (LEGACY_DURABLE_RECEIPT_SCHEMA_VERSION,),
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
                UPDATE automation_research_requests
                   SET core_run_id = research_id
                 WHERE core_run_id IS NULL OR core_run_id = ''
                """
            )
            connection.execute(
                """
                UPDATE automation_research_requests
                   SET attempt_started_at = started_at
                 WHERE status = 'running' AND attempt_started_at IS NULL
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
        result["request"] = _json_load(result.pop("request_json", None))
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
        self,
        request_id: str,
        fingerprint: str,
        research_id: str,
        *,
        request_payload: dict[str, Any] | None = None,
        capacity: AutomationCapacity | None = None,
        deadlines: AutomationDeadlines | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically reserve a request ID, returning its canonical row."""

        now = utc_now_iso()
        lease_token = uuid.uuid4().hex
        core_run_id = contracts.attempt_research_id(research_id, 1, lease_token)
        deadlines = deadlines or contracts._automation_deadlines()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                """
                SELECT * FROM automation_research_requests
                 WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if existing_row is not None:
                connection.commit()
                existing = self._row(existing_row)
                if existing is None:  # pragma: no cover - SQLite invariant defense
                    raise RuntimeError("Automation request reservation was not readable")
                return existing, False

            status = "running"
            attempt_started_at: str | None = now
            deadline_at: str | None = contracts._deadline_iso(
                now, deadlines.overall_seconds
            )
            if capacity is not None:
                running_count = int(
                    connection.execute(
                        """
                        SELECT count(*) FROM automation_research_requests
                         WHERE status = 'running' AND response_json IS NULL
                        """
                    ).fetchone()[0]
                )
                queued_count = int(
                    connection.execute(
                        """
                        SELECT count(*) FROM automation_research_requests
                         WHERE status = 'queued' AND response_json IS NULL
                        """
                    ).fetchone()[0]
                )
                if (
                    running_count >= capacity.max_concurrent
                    or queued_count > 0
                ):
                    if queued_count >= capacity.max_queued:
                        connection.rollback()
                        raise AutomationAdmissionSaturated(
                            "Mastery Research running allowance and durable queue are full"
                        )
                    status = "queued"
                    attempt_started_at = None
                    deadline_at = None
            reserved_core_run_id = core_run_id if status == "running" else None
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO automation_research_requests (
                    request_id, request_fingerprint, research_id, core_run_id,
                    request_json, lease_token, lease_generation, status,
                    started_at, attempt_started_at, deadline_at,
                    overall_timeout_seconds, deep_timeout_seconds,
                    report_timeout_seconds, receipt_schema_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    fingerprint,
                    research_id,
                    reserved_core_run_id,
                    _json_dump(request_payload) if request_payload is not None else None,
                    lease_token,
                    status,
                    now,
                    attempt_started_at,
                    deadline_at,
                    deadlines.overall_seconds,
                    deadlines.deep_research_seconds,
                    deadlines.report_seconds,
                    DURABLE_RECEIPT_SCHEMA_VERSION,
                    now,
                ),
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
            current = connection.execute(
                """
                SELECT research_id, lease_generation, overall_timeout_seconds
                  FROM automation_research_requests
                 WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if current is None:
                connection.rollback()
                return None
            generation = int(current["lease_generation"] or 1) + 1
            overall_seconds = int(
                current["overall_timeout_seconds"]
                or contracts._automation_deadlines().overall_seconds
            )
            core_run_id = contracts.attempt_research_id(
                str(current["research_id"]), generation, lease_token
            )
            cursor = connection.execute(
                """
                UPDATE automation_research_requests
                   SET lease_token = ?,
                       lease_generation = ?,
                       core_run_id = ?,
                       attempt_started_at = ?,
                       deadline_at = ?,
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
                    generation,
                    core_run_id,
                    now,
                    contracts._deadline_iso(now, overall_seconds),
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

    def claim_queued(
        self,
        request_id: str,
        fingerprint: str,
        *,
        capacity: AutomationCapacity,
    ) -> dict[str, Any] | None:
        """Promote one exact queued replay when a durable running slot is free."""

        now = utc_now_iso()
        lease_token = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running_count = int(
                connection.execute(
                    """
                    SELECT count(*) FROM automation_research_requests
                     WHERE status = 'running' AND response_json IS NULL
                    """
                ).fetchone()[0]
            )
            if running_count >= capacity.max_concurrent:
                connection.rollback()
                return None
            row = connection.execute(
                """
                SELECT research_id, lease_generation, overall_timeout_seconds
                  FROM automation_research_requests
                 WHERE request_id = ? AND request_fingerprint = ?
                   AND status = 'queued' AND response_json IS NULL
                """,
                (request_id, fingerprint),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            overall_seconds = int(
                row["overall_timeout_seconds"]
                or contracts._automation_deadlines().overall_seconds
            )
            core_run_id = contracts.attempt_research_id(
                str(row["research_id"]),
                int(row["lease_generation"] or 1),
                lease_token,
            )
            cursor = connection.execute(
                """
                UPDATE automation_research_requests
                   SET status = 'running', lease_token = ?,
                       core_run_id = ?, attempt_started_at = ?,
                       deadline_at = ?, updated_at = ?
                 WHERE request_id = ? AND request_fingerprint = ?
                   AND status = 'queued' AND response_json IS NULL
                """,
                (
                    lease_token,
                    core_run_id,
                    now,
                    contracts._deadline_iso(now, overall_seconds),
                    now,
                    request_id,
                    fingerprint,
                ),
            )
            promoted = (
                connection.execute(
                    "SELECT * FROM automation_research_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if cursor.rowcount == 1
                else None
            )
            connection.commit()
        return self._row(promoted)

    def claim_next_queued(
        self,
        *,
        capacity: AutomationCapacity,
    ) -> dict[str, Any] | None:
        """Atomically admit the oldest durable queued operation."""

        now = utc_now_iso()
        lease_token = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running_count = int(
                connection.execute(
                    """
                    SELECT count(*) FROM automation_research_requests
                     WHERE status = 'running' AND response_json IS NULL
                    """
                ).fetchone()[0]
            )
            if running_count >= capacity.max_concurrent:
                connection.rollback()
                return None
            queued = connection.execute(
                """
                SELECT request_id, research_id, lease_generation,
                       overall_timeout_seconds
                  FROM automation_research_requests
                 WHERE status = 'queued' AND response_json IS NULL
                 ORDER BY started_at, request_id
                 LIMIT 1
                """
            ).fetchone()
            if queued is None:
                connection.rollback()
                return None
            overall_seconds = int(
                queued["overall_timeout_seconds"]
                or contracts._automation_deadlines().overall_seconds
            )
            core_run_id = contracts.attempt_research_id(
                str(queued["research_id"]),
                int(queued["lease_generation"] or 1),
                lease_token,
            )
            cursor = connection.execute(
                """
                UPDATE automation_research_requests
                   SET status = 'running', lease_token = ?,
                       core_run_id = ?, attempt_started_at = ?,
                       deadline_at = ?, updated_at = ?
                 WHERE request_id = ? AND status = 'queued'
                   AND response_json IS NULL
                """,
                (
                    lease_token,
                    core_run_id,
                    now,
                    contracts._deadline_iso(now, overall_seconds),
                    now,
                    queued["request_id"],
                ),
            )
            promoted = (
                connection.execute(
                    "SELECT * FROM automation_research_requests WHERE request_id = ?",
                    (queued["request_id"],),
                ).fetchone()
                if cursor.rowcount == 1
                else None
            )
            connection.commit()
        return self._row(promoted)

    def claim_next_stale(self, *, stale_before: str) -> dict[str, Any] | None:
        """Reclaim the oldest abandoned running slot for autonomous queue recovery."""

        now = utc_now_iso()
        lease_token = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale = connection.execute(
                """
                SELECT request_id, request_fingerprint, research_id, core_run_id,
                       lease_token, lease_generation, overall_timeout_seconds
                  FROM automation_research_requests
                 WHERE status = 'running' AND response_json IS NULL
                   AND updated_at <= ?
                 ORDER BY updated_at, request_id
                 LIMIT 1
                """,
                (stale_before,),
            ).fetchone()
            if stale is None:
                connection.rollback()
                return None
            generation = int(stale["lease_generation"] or 1) + 1
            core_run_id = contracts.attempt_research_id(
                str(stale["research_id"]), generation, lease_token
            )
            overall_seconds = int(
                stale["overall_timeout_seconds"]
                or contracts._automation_deadlines().overall_seconds
            )
            cursor = connection.execute(
                """
                UPDATE automation_research_requests
                   SET lease_token = ?, lease_generation = ?, core_run_id = ?,
                       attempt_started_at = ?, deadline_at = ?, updated_at = ?
                 WHERE request_id = ? AND request_fingerprint = ?
                   AND lease_token = ? AND status = 'running'
                   AND response_json IS NULL AND updated_at <= ?
                """,
                (
                    lease_token,
                    generation,
                    core_run_id,
                    now,
                    contracts._deadline_iso(now, overall_seconds),
                    now,
                    stale["request_id"],
                    stale["request_fingerprint"],
                    stale["lease_token"],
                    stale_before,
                ),
            )
            reclaimed = (
                connection.execute(
                    "SELECT * FROM automation_research_requests WHERE request_id = ?",
                    (stale["request_id"],),
                ).fetchone()
                if cursor.rowcount == 1
                else None
            )
            connection.commit()
        result = self._row(reclaimed)
        if result is not None:
            result["prior_core_run_id"] = stale["core_run_id"] or stale["research_id"]
        return result

    def has_queued(self) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM automation_research_requests
                 WHERE status = 'queued' AND response_json IS NULL
                 LIMIT 1
                """
            ).fetchone()
        return row is not None

    def has_running(self) -> bool:
        """Return whether a nonterminal lease still needs supervision."""

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM automation_research_requests
                 WHERE status = 'running' AND response_json IS NULL
                 LIMIT 1
                """
            ).fetchone()
        return row is not None

    def adopt_terminal_core_run(
        self,
        request_id: str,
        fingerprint: str,
        lease_token: str,
        core_run_id: str,
    ) -> bool:
        """Bind a reclaimed receipt back to an already-terminal fenced core run."""

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE automation_research_requests
                   SET core_run_id = ?, updated_at = ?
                 WHERE request_id = ? AND request_fingerprint = ?
                   AND lease_token = ? AND status = 'running'
                   AND response_json IS NULL
                """,
                (
                    core_run_id,
                    utc_now_iso(),
                    request_id,
                    fingerprint,
                    lease_token,
                ),
            )
        return cursor.rowcount == 1

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


def get_automation_research_store() -> AutomationResearchStore:
    """Return the cached store for the currently configured ledger path."""

    global _store
    path = get_research_run_store_path()
    with _store_guard:
        if _store is None or _store.path != path:
            _store = AutomationResearchStore(path)
        return _store


def _clear_automation_research_store_cache() -> None:
    """Forget the process-local store handle without changing durable receipts."""

    global _store
    with _store_guard:
        _store = None


__all__ = [
    "AutomationLeaseLost",
    "AutomationResearchStore",
    "_clear_automation_research_store_cache",
    "_json_dump",
    "_json_load",
    "_store",
    "_store_guard",
    "get_automation_research_store",
]
