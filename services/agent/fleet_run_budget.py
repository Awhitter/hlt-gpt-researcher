"""Small native-Hermes adapter for explicitly budgeted scheduled runs.

Ordinary Slack/API work is unaffected. The scheduler overlay attaches this
contract only when a job carries ``hlt_run_budget``. Native usage counters
shrink the next output allowance; a grace call cannot bypass the run ceiling.
"""
from __future__ import annotations

import json
import time
from typing import Any

CANARY_BUDGET = {
    "max_iterations": 4,
    "max_output_tokens": 1200,
    "max_input_bytes": 64000,
    "max_seconds": 120,
}


def budget_kwargs(job: dict[str, Any], max_iterations: int) -> dict[str, Any]:
    requested = job.get("hlt_run_budget")
    if requested is None:
        return {"max_iterations": max_iterations}
    if not isinstance(requested, dict):
        raise ValueError("Scheduled run budget must be an object")
    limits = {}
    for key, ceiling in CANARY_BUDGET.items():
        value = requested.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"Invalid scheduled run budget: {key}")
        limits[key] = min(value, ceiling)
    return {
        "max_iterations": min(max_iterations, limits["max_iterations"]),
        "max_tokens": limits["max_output_tokens"],
        "run_budget_seconds": limits["max_seconds"],
    }


def attach_budget(agent: Any, job: dict[str, Any]) -> None:
    if job.get("hlt_run_budget") is None:
        return
    # Validate again here so the hook cannot be attached with an invalid job.
    budget_kwargs(job, agent.max_iterations)
    if getattr(agent, "api_mode", None) == "codex_app_server":
        raise ValueError("Scheduled token limits require the native provider loop")
    agent._hlt_scheduled_budget = {
        key: min(job["hlt_run_budget"][key], ceiling)
        for key, ceiling in CANARY_BUDGET.items()
    }
    agent._hlt_scheduled_started = time.monotonic()
    agent._hlt_scheduled_input_bytes = 0
    agent._hlt_scheduled_reserved_output = 0
    agent._hlt_scheduled_observed_output = 0
    agent._hlt_scheduled_requests = 0


def admit_iteration(agent: Any, messages: list[dict], api_calls: int) -> bool:
    limits = getattr(agent, "_hlt_scheduled_budget", None)
    if limits is None:
        return True
    remaining = limits["max_output_tokens"] - max(
        int(getattr(agent, "session_output_tokens", 0) or 0),
        int(getattr(agent, "session_completion_tokens", 0) or 0),
    )
    if (
        remaining <= 0
        or api_calls >= limits["max_iterations"]
        or time.monotonic() - agent._hlt_scheduled_started >= limits["max_seconds"]
    ):
        return False
    agent.max_tokens = min(agent.max_tokens, remaining)
    return True


def admit_request(agent: Any, api_kwargs: dict[str, Any]) -> bool:
    """Clamp the actual provider payload, including inner retries and boosts.

    If an attempt failed before usage was recorded, keep its whole reservation:
    unknown provider billing is not permission for another unattended attempt.
    Normal successful tool calls release the unused reservation from usage.
    """
    limits = getattr(agent, "_hlt_scheduled_budget", None)
    if limits is None:
        return True
    observed = max(
        int(getattr(agent, "session_output_tokens", 0) or 0),
        int(getattr(agent, "session_completion_tokens", 0) or 0),
    )
    reserved = agent._hlt_scheduled_reserved_output
    if observed > agent._hlt_scheduled_observed_output:
        reserved = 0
    remaining = limits["max_output_tokens"] - observed - reserved
    request_bytes = len(json.dumps(
        api_kwargs, ensure_ascii=False, default=str,
    ).encode("utf-8"))
    used_bytes = agent._hlt_scheduled_input_bytes + request_bytes
    if (
        remaining <= 0
        or agent._hlt_scheduled_requests >= limits["max_iterations"]
        or used_bytes > limits["max_input_bytes"]
        or time.monotonic() - agent._hlt_scheduled_started >= limits["max_seconds"]
    ):
        return False
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if key in api_kwargs:
            api_kwargs[key] = min(int(api_kwargs[key]), remaining)
    # The scheduled Grok transport consumes an explicit output cap. Codex's
    # subscription wire deliberately omits unsupported caps, so it cannot serve
    # this budgeted job. Ordinary Codex/Slack work is unaffected.
    caps = [api_kwargs[key] for key in ("max_tokens", "max_completion_tokens", "max_output_tokens") if key in api_kwargs]
    if not caps:
        return False
    agent.max_tokens = min(agent.max_tokens, remaining)
    agent._hlt_scheduled_input_bytes = used_bytes
    agent._hlt_scheduled_requests += 1
    agent._hlt_scheduled_reserved_output = max(caps)
    agent._hlt_scheduled_observed_output = observed
    return True
