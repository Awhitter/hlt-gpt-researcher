"""Typed provider recovery facts shared by the native run and its K2 adapter.

These facts describe an observed model failure. They never authorize a new
request, clear quota state, or assert that a failed turn is still executing.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def recovery_from_result(result: Mapping[str, Any]) -> dict[str, Any] | None:
    reason = result.get("failure_reason")
    if (
        result.get("failed") is not True
        or result.get("interrupted")
        or reason not in {"rate_limit", "billing"}
        or (reason == "billing" and result.get("billing_unverified"))
    ):
        return None
    recovery: dict[str, Any] = {
        "state": "waiting_for_provider",
        "reason": reason,
        "automaticResume": False,
        "continuationRequired": True,
    }
    reset = result.get("failure_resets_at")
    if (
        isinstance(reset, (int, float)) and not isinstance(reset, bool)
        and math.isfinite(reset) and 0 < reset <= 8_640_000_000_000
    ):
        recovery["retryAt"] = int(reset * 1000)
    return recovery


def sanitize_provider_recovery(value: Any, *, automatic: bool = False) -> dict[str, Any] | None:
    """Persist the typed contract only when it matches the actual native state."""
    if (
        not isinstance(value, Mapping)
        or value.get("state") != "waiting_for_provider"
        or value.get("reason") not in {"rate_limit", "billing"}
        or value.get("automaticResume") is not automatic
        or value.get("continuationRequired") is not (not automatic)
    ):
        return None
    clean = {
        key: value[key]
        for key in ("state", "reason", "automaticResume", "continuationRequired")
    }
    retry_at = value.get("retryAt")
    if (
        isinstance(retry_at, int) and not isinstance(retry_at, bool)
        and 0 < retry_at <= 8_640_000_000_000_000
    ):
        clean["retryAt"] = retry_at
    return clean


def sanitize_terminal_recovery(value: Any) -> dict[str, Any] | None:
    return sanitize_provider_recovery(value)
