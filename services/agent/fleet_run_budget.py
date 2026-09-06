"""Native-Hermes limits for explicitly budgeted scheduled and K2 runs.

Ordinary Slack/API work is unaffected. The scheduler overlay attaches this
contract only when a job carries ``hlt_run_budget``. Native usage counters
reconcile conservative input reservations and shrink the next output allowance;
a grace call cannot bypass the run ceiling. K2 hosted runs can separately
opt into a turn ceiling without inheriting canary token limits or model policy.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

CANARY_BUDGET = {
    "max_iterations": 4,
    "max_output_tokens": 1200,
    "max_input_tokens": 64000,
    "max_seconds": 120,
}


def validate_hosted_turn_limit(value: Any, *, field: str = "maxTurns") -> int | None:
    if value is None:
        return None
    # Matches K2's existing StarterExecutionPlanV1, not a new host-wide cap.
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
        raise ValueError(f"{field} must be an integer from 1 to 12")
    return value


def attach_hosted_turn_limit(agent: Any, max_turns: int | None) -> None:
    limit = validate_hosted_turn_limit(max_turns)
    if limit is None:
        return
    if getattr(agent, "api_mode", None) == "codex_app_server":
        raise ValueError("Hosted turn limits require the native provider loop")
    agent.max_iterations = min(agent.max_iterations, limit)
    agent._hlt_hosted_max_turns = agent.max_iterations


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
    agent._hlt_scheduled_reserved_input = 0
    agent._hlt_scheduled_last_input_reservation = 0
    agent._hlt_scheduled_observed_input = 0
    agent._hlt_scheduled_reserved_output = 0
    agent._hlt_scheduled_observed_output = 0
    agent._hlt_scheduled_requests = 0


def _observed_input_tokens(agent: Any) -> int:
    # Pinned Hermes CanonicalUsage.prompt_tokens includes uncached input plus
    # BOTH cache buckets. session_input_tokens alone excludes cached input.
    # Prefer the inclusive native counter, with equivalent bucket fallback;
    # max avoids charging caches twice when both forms are available.
    return max(
        int(getattr(agent, "session_prompt_tokens", 0) or 0),
        sum(int(getattr(agent, key, 0) or 0) for key in (
            "session_input_tokens", "session_cache_read_tokens", "session_cache_write_tokens",
        )),
    )


def admit_iteration(agent: Any, messages: list[dict], api_calls: int) -> bool:
    hosted_limit = getattr(agent, "_hlt_hosted_max_turns", None)
    if hosted_limit is not None and api_calls >= hosted_limit:
        return False
    limits = getattr(agent, "_hlt_scheduled_budget", None)
    if limits is None:
        return True
    remaining = limits["max_output_tokens"] - max(
        int(getattr(agent, "session_output_tokens", 0) or 0),
        int(getattr(agent, "session_completion_tokens", 0) or 0),
    )
    if (
        remaining <= 0
        or _observed_input_tokens(agent) >= limits["max_input_tokens"]
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
    Input reserves one token per serialized UTF-8 byte (not a tokenizer count),
    then reconciles that latest reservation to cache-inclusive provider usage.
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
    observed_input = _observed_input_tokens(agent)
    reserved_input = agent._hlt_scheduled_reserved_input
    if observed_input > agent._hlt_scheduled_observed_input:
        # Settle only the latest request. Older attempts without input usage
        # remain charged even if a later response does provide a receipt.
        reserved_input = max(0, reserved_input - agent._hlt_scheduled_last_input_reservation)
    projected_input = observed_input + reserved_input + request_bytes
    elapsed = time.monotonic() - agent._hlt_scheduled_started

    def reject(reason: str) -> bool:
        # Numeric receipt only: retain enough to diagnose a budget refusal
        # without logging prompts, tool schemas, arguments, or credentials.
        detail = {
            "reason": reason,
            "requests": agent._hlt_scheduled_requests,
            "max_iterations": limits["max_iterations"],
            "request_bytes": request_bytes,
            "observed_input_tokens": observed_input,
            "reserved_input_tokens": reserved_input,
            "projected_input_tokens": projected_input,
            "max_input_tokens": limits["max_input_tokens"],
            "remaining_output_tokens": remaining,
            "elapsed_seconds": round(elapsed, 3),
            "max_seconds": limits["max_seconds"],
        }
        agent._hlt_scheduled_rejection = detail
        logger.warning("Scheduled run request refused: %s", json.dumps(detail, sort_keys=True))
        return False

    if remaining <= 0:
        return reject("output_tokens")
    if agent._hlt_scheduled_requests >= limits["max_iterations"]:
        return reject("request_count")
    if projected_input > limits["max_input_tokens"]:
        return reject("input_reservation")
    if elapsed >= limits["max_seconds"]:
        return reject("wall_time")
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if key in api_kwargs:
            api_kwargs[key] = min(int(api_kwargs[key]), remaining)
    # The scheduled Grok transport consumes an explicit output cap. Codex's
    # subscription wire deliberately omits unsupported caps, so it cannot serve
    # this budgeted job. Ordinary Codex/Slack work is unaffected.
    caps = [api_kwargs[key] for key in ("max_tokens", "max_completion_tokens", "max_output_tokens") if key in api_kwargs]
    if not caps:
        return reject("output_cap_missing")
    agent.max_tokens = min(agent.max_tokens, remaining)
    agent._hlt_scheduled_reserved_input = reserved_input + request_bytes
    agent._hlt_scheduled_last_input_reservation = request_bytes
    agent._hlt_scheduled_observed_input = observed_input
    agent._hlt_scheduled_requests += 1
    agent._hlt_scheduled_reserved_output = max(caps)
    agent._hlt_scheduled_observed_output = observed
    return True
