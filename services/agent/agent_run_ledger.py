"""Durable, idempotent admission ledger for externally-triggered agent runs.

The public K2 hook and Hermes' native run API are two different durability
domains.  A provider run can be accepted even when the wrapper process dies
before returning its provider id.  This ledger writes the K2 admission first,
then records the dispatch boundary and native id in separate durable
transactions.  A replay can therefore resume a definitely-undispatched row,
but it can never guess that an ambiguous dispatch is safe to repeat.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ADMISSION_STATES = frozenset({"queued", "dispatching", "provider_bound", "terminal"})
TERMINAL_PROVIDER_STATES = frozenset({"completed", "failed", "cancelled"})
MAX_OUTPUT_CHARS = 50_000
MAX_ERROR_CHARS = 2_000
MAX_USAGE_ITEMS = 100

_PREFIX_SECRET_RE = re.compile(
    r"(?i)\b(?:sk-(?:or-v1-)?|xox[baprs]-|gh[pousr]_|pat_|Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"
)
_NAMED_SECRET_RE = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|authorization|"
    r"password|client[_ -]?secret)\b\s*[:=]\s*([^\s,;]+)"
)


class AdmissionConflict(ValueError):
    """The same K2 id or session was replayed with different semantics."""


class AdmissionStateError(RuntimeError):
    """A ledger transition violated the monotonic admission state machine."""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def wrapper_run_id(k2_run_id: str) -> str:
    """The fleet-wide deterministic K2-to-host run id contract."""
    normalized = k2_run_id.replace("-", "")
    if not re.fullmatch(r"[0-9a-f]{32}", normalized):
        raise ValueError("katailyst_run_id must be a canonical lowercase UUID")
    return f"run_{normalized}"


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash the exact validated admission without persisting mission content."""
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def redact_bounded_text(value: Any, max_chars: int) -> str:
    text = str(value or "")
    text = _PREFIX_SECRET_RE.sub("[redacted]", text)
    text = _NAMED_SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    if len(text) <= max_chars:
        return text
    marker = "\n[truncated by Cleo host]"
    return f"{text[: max(0, max_chars - len(marker))]}{marker}"


def sanitize_usage(value: Any) -> Any:
    """Keep bounded numeric telemetry; credentials and provider blobs stay out."""

    remaining = MAX_USAGE_ITEMS

    def visit(item: Any, depth: int = 0) -> Any:
        nonlocal remaining
        if remaining <= 0 or depth > 5:
            return None
        if isinstance(item, bool) or item is None:
            remaining -= 1
            return item
        if isinstance(item, int):
            remaining -= 1
            return item
        if isinstance(item, float):
            remaining -= 1
            return item if math.isfinite(item) else None
        if isinstance(item, Mapping):
            out: dict[str, Any] = {}
            for key, nested in item.items():
                if remaining <= 0:
                    break
                name = str(key)[:120]
                if re.search(
                    r"(?i)(api.?key|access.?token|refresh.?token|authorization|password|secret)",
                    name,
                ):
                    out[name] = "[redacted]"
                    remaining -= 1
                    continue
                sanitized = visit(nested, depth + 1)
                if sanitized is not None:
                    out[name] = sanitized
            return out
        if isinstance(item, (list, tuple)):
            out = []
            for nested in item:
                if remaining <= 0:
                    break
                sanitized = visit(nested, depth + 1)
                if sanitized is not None:
                    out.append(sanitized)
            return out
        # Provider usage strings are labels at best and credentials at worst.
        return None

    return visit(value)


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    raw_usage = value.pop("usage_json", None)
    try:
        value["usage"] = json.loads(raw_usage) if raw_usage else None
    except json.JSONDecodeError:
        value["usage"] = None
    return value


class AgentRunLedger:
    """SQLite-backed monotonic admission and provider-binding state."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=8.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=8000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_run_admissions (
                    wrapper_run_id TEXT PRIMARY KEY,
                    k2_run_id TEXT NOT NULL UNIQUE,
                    session_key TEXT NOT NULL UNIQUE,
                    org_id TEXT NOT NULL,
                    agent_ref TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    admission_status TEXT NOT NULL,
                    provider_run_id TEXT UNIQUE,
                    provider_status TEXT,
                    recovery_code TEXT,
                    output_text TEXT,
                    error_text TEXT,
                    usage_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    terminal_at TEXT,
                    CHECK (admission_status IN ('queued', 'dispatching', 'provider_bound', 'terminal'))
                );
                CREATE INDEX IF NOT EXISTS idx_agent_run_admissions_status
                    ON agent_run_admissions(admission_status);
                """
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def probe(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"ready": True, "schema_version": SCHEMA_VERSION}

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_run_admissions WHERE wrapper_run_id = ?",
                (run_id,),
            ).fetchone()
        return _row_dict(row)

    def admit(
        self,
        *,
        k2_run_id: str,
        session_key: str,
        org_id: str,
        agent_ref: str,
        fingerprint: str,
    ) -> tuple[dict[str, Any], bool]:
        run_id = wrapper_run_id(k2_run_id)
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM agent_run_admissions
                 WHERE wrapper_run_id = ? OR k2_run_id = ? OR session_key = ?
                 LIMIT 1
                """,
                (run_id, k2_run_id, session_key),
            ).fetchone()
            if row is not None:
                existing = _row_dict(row)
                assert existing is not None
                exact = (
                    existing["wrapper_run_id"] == run_id
                    and existing["k2_run_id"] == k2_run_id
                    and existing["session_key"] == session_key
                    and existing["org_id"] == org_id
                    and existing["agent_ref"] == agent_ref
                    and existing["request_fingerprint"] == fingerprint
                )
                if not exact:
                    raise AdmissionConflict(
                        "katailyst run/session was already admitted with different semantics"
                    )
                return existing, False
            conn.execute(
                """
                INSERT INTO agent_run_admissions (
                    wrapper_run_id, k2_run_id, session_key, org_id, agent_ref,
                    request_fingerprint, admission_status, provider_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 'queued', ?, ?)
                """,
                (
                    run_id,
                    k2_run_id,
                    session_key,
                    org_id,
                    agent_ref,
                    fingerprint,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_run_admissions WHERE wrapper_run_id = ?",
                (run_id,),
            ).fetchone()
        admitted = _row_dict(row)
        assert admitted is not None
        return admitted, True

    def claim_dispatch(self, run_id: str) -> bool:
        """Cross the no-redispatch boundary exactly once."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_run_admissions
                   SET admission_status = 'dispatching',
                       provider_status = 'unknown',
                       recovery_code = NULL,
                       updated_at = ?
                 WHERE wrapper_run_id = ? AND admission_status = 'queued'
                """,
                (utc_now_iso(), run_id),
            )
        return cursor.rowcount == 1

    def mark_dispatch_ambiguous(self, run_id: str, detail: Any = "") -> None:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_run_admissions
                   SET provider_status = 'unknown',
                       recovery_code = 'provider_admission_ambiguous',
                       error_text = ?,
                       updated_at = ?
                 WHERE wrapper_run_id = ? AND admission_status = 'dispatching'
                """,
                (redact_bounded_text(detail, MAX_ERROR_CHARS), utc_now_iso(), run_id),
            )
        if cursor.rowcount != 1:
            raise AdmissionStateError(
                "cannot mark a non-dispatching admission ambiguous"
            )

    def bind_provider(self, run_id: str, provider_run_id: str) -> None:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_run_admissions
                   SET admission_status = 'provider_bound',
                       provider_run_id = ?,
                       provider_status = 'queued',
                       recovery_code = NULL,
                       error_text = NULL,
                       updated_at = ?
                 WHERE wrapper_run_id = ? AND admission_status = 'dispatching'
                """,
                (provider_run_id, utc_now_iso(), run_id),
            )
        if cursor.rowcount != 1:
            raise AdmissionStateError("cannot bind provider outside dispatching")

    def note_provider_status(self, run_id: str, provider_status: str) -> None:
        if provider_status in TERMINAL_PROVIDER_STATES:
            raise AdmissionStateError("terminal provider state requires mark_terminal")
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_run_admissions
                   SET provider_status = ?, recovery_code = NULL, updated_at = ?
                 WHERE wrapper_run_id = ? AND admission_status = 'provider_bound'
                """,
                (provider_status, utc_now_iso(), run_id),
            )
        if cursor.rowcount != 1:
            raise AdmissionStateError("cannot update provider status before binding")

    def note_provider_unknown(self, run_id: str, code: str, detail: Any = "") -> None:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_run_admissions
                   SET provider_status = 'unknown', recovery_code = ?,
                       error_text = ?, updated_at = ?
                 WHERE wrapper_run_id = ? AND admission_status = 'provider_bound'
                """,
                (
                    redact_bounded_text(code, 120),
                    redact_bounded_text(detail, MAX_ERROR_CHARS),
                    utc_now_iso(),
                    run_id,
                ),
            )
        if cursor.rowcount != 1:
            raise AdmissionStateError("cannot mark provider unknown before binding")

    def mark_terminal(
        self,
        run_id: str,
        provider_status: str,
        *,
        output: Any = "",
        error: Any = "",
        usage: Any = None,
    ) -> None:
        if provider_status not in TERMINAL_PROVIDER_STATES:
            raise ValueError("provider_status is not terminal")
        now = utc_now_iso()
        sanitized_usage = sanitize_usage(usage)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_run_admissions
                   SET admission_status = 'terminal', provider_status = ?,
                       recovery_code = NULL, output_text = ?, error_text = ?,
                       usage_json = ?, updated_at = ?, terminal_at = ?
                 WHERE wrapper_run_id = ?
                   AND admission_status IN ('dispatching', 'provider_bound')
                """,
                (
                    provider_status,
                    redact_bounded_text(output, MAX_OUTPUT_CHARS),
                    redact_bounded_text(error, MAX_ERROR_CHARS),
                    json.dumps(sanitized_usage, ensure_ascii=False),
                    now,
                    now,
                    run_id,
                ),
            )
        if cursor.rowcount != 1:
            current = self.get(run_id)
            if not current or current.get("admission_status") != "terminal":
                raise AdmissionStateError("cannot terminalize unknown admission state")
