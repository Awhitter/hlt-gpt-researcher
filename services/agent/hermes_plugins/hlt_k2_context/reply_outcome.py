"""Report each finished Slack or API turn to Katailyst2 as one of Cleo's runs.

On 14-15 Oct the hosted agents failed every reply for about 20 hours while K2
called them healthy: it only saw that the servers were up. ``agents.health`` now
reads replies from K2's run ledger, so the host has to put them there. This is
the Hermes twin of openclaw-hq's ``katailyst-registry/reply-outcome.ts``: the
outcome only (worked or failed, which model, how long, the error line), never
the conversation, sent from a daemon thread so it never holds up or changes a
reply.

Only replies count. Cron jobs and fleet housekeeping run on their own
platforms; a subagent's turn belongs to the turn that started it; a turn the
user interrupted (a new message, /stop) did not finish either way.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .runtime_context import MCP_PROTOCOL_VERSION, _post

logger = logging.getLogger(__name__)

RECORD_TOOL_NAMES = ("agents.run.record", "agents_run_record")
REPORTED_PLATFORMS = {"slack": "slack", "api_server": "api"}
RECORD_TIMEOUT_SECONDS = 10.0
MAX_ERROR_CHARS = 300
_MAX_OPEN_TURNS = 256
# Provider errors sometimes echo a key or bearer; keep the error line, drop
# anything shaped like one. Same pattern as the OpenClaw reporter.
_SECRET_SHAPED = re.compile(
    r"\b(?:sk|pk|rk|xox[abpr])[-_][A-Za-z0-9_-]{8,}|\bBearer\s+\S+|\b[A-Za-z0-9_-]{40,}\b"
)
_TURN_STARTS: dict[str, float] = {}
_TURN_STARTS_LOCK = threading.Lock()


def note_turn_started(turn_id: str, *, now: float | None = None) -> None:
    """Remember when a turn began so its record can say how long it took."""
    if not turn_id:
        return
    with _TURN_STARTS_LOCK:
        if len(_TURN_STARTS) >= _MAX_OPEN_TURNS:
            _TURN_STARTS.pop(next(iter(_TURN_STARTS)))
        _TURN_STARTS[turn_id] = time.monotonic() if now is None else now


def _turn_seconds(turn_id: str, now: float | None) -> int | None:
    with _TURN_STARTS_LOCK:
        started = _TURN_STARTS.pop(turn_id, None)
    if started is None:
        return None
    return max(0, round((time.monotonic() if now is None else now) - started))


def redact_error(value: Any) -> str:
    text = _SECRET_SHAPED.sub("[redacted]", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:MAX_ERROR_CHARS]


def reply_outcome_record(
    agent_ref: str,
    *,
    session_id: str = "",
    turn_id: str = "",
    completed: bool = False,
    failed: bool = False,
    interrupted: bool = False,
    turn_exit_reason: Any = "",
    model: str = "",
    platform: str = "",
    monotonic_now: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The ``agents.run.record`` arguments for one finished turn, or None."""
    turn_id = str(turn_id or "")
    # Pop the start time first, so turns that are never reported (cron, an
    # interrupt) do not linger in _TURN_STARTS.
    seconds = _turn_seconds(turn_id, monotonic_now)
    where = REPORTED_PLATFORMS.get(str(platform or "").strip().lower())
    if not agent_ref or not turn_id or not where or interrupted:
        return None
    # A Slack turn that reaches its deliberate turn cap still answers: Hermes
    # asks the model for a summary and delivers it, but reports completed=False.
    # Counting that as a failure would flag Cleo "Replies are failing" in K2.
    hit_turn_cap = str(turn_exit_reason or "").startswith("max_iterations_reached(")
    worked = (bool(completed) or hit_turn_cap) and not failed
    model_name = str(model or "") or "unknown model"
    took = "" if seconds is None else f" in {seconds}s"
    finished = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def iso(moment: datetime) -> str:
        return moment.isoformat().replace("+00:00", "Z")

    error = redact_error(turn_exit_reason) or "no error message"
    record: dict[str, Any] = {
        "agentRef": agent_ref,
        "mission": f"Reply on {where}",
        "outcome": "completed" if worked else "failed",
        "runtime": "hermes",
        "summary": (
            f"Replied{took} on {model_name}"
            + (" (summary at the turn cap)." if hit_turn_cap and not completed else ".")
            if worked
            else f"Reply failed{took} on {model_name}: {error}"
        ),
        # A K2-dispatched run's session is hook:k2:<runId>, which ties this
        # record to K2's own row for the same mission.
        "externalRef": str(session_id or turn_id)[:500],
        "completedAt": iso(finished),
        "idempotencyKey": f"turn:{turn_id}"[:200],
    }
    if seconds is not None:
        record["startedAt"] = iso(finished - timedelta(seconds=seconds))
    return record


def record_run(url: str, token: str, record: Mapping[str, Any]) -> None:
    """Call agents.run.record once over streamable HTTP; raise on any failure."""
    deadline = time.monotonic() + RECORD_TIMEOUT_SECONDS
    request_id = 0

    def rpc(method: str, params: dict[str, Any], session_id: str = ""):
        nonlocal request_id
        request_id += 1
        return _post(
            url,
            token,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            session_id=session_id,
            timeout=max(0.5, deadline - time.monotonic()),
        )

    initialized, session_id, _ = rpc(
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "hlt-hermes-reply-outcome", "version": "1.0.0"},
        },
    )
    if initialized.get("error"):
        raise RuntimeError(str(initialized["error"])[:200])
    last_error = ""
    for name in RECORD_TOOL_NAMES:
        called, _, _ = rpc(
            "tools/call", {"name": name, "arguments": dict(record)}, session_id
        )
        result = called.get("result") if isinstance(called.get("result"), Mapping) else {}
        if not called.get("error") and result.get("isError") is not True:
            return
        last_error = str(called.get("error") or result.get("content") or "isError")
        # Only an unknown name is worth the alias; any other refusal is final.
        if "not found" not in last_error.lower() and "unknown tool" not in last_error.lower():
            break
    raise RuntimeError(last_error[:200])


def report_in_background(
    url: str,
    token: str,
    record: Mapping[str, Any],
    *,
    send: Callable[[str, str, Mapping[str, Any]], None] = record_run,
) -> threading.Thread | None:
    """Send the record from a daemon thread; a failure is one warning line."""
    if not url or not token:
        return None

    def run() -> None:
        try:
            send(url, token, record)
        except Exception as exc:  # noqa: BLE001 - reporting never affects a reply
            logger.warning(
                "could not report a %s reply to Katailyst2: %s",
                record.get("outcome"),
                redact_error(exc),
            )

    thread = threading.Thread(target=run, name="k2-reply-outcome", daemon=True)
    thread.start()
    return thread


def _reset_for_tests() -> None:
    with _TURN_STARTS_LOCK:
        _TURN_STARTS.clear()
