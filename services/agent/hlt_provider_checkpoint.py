"""Private continuation snapshots at a settled native model-call boundary.

The native run owns admission, Stop, leases, and delivery. This module only
serializes/restores its already-admitted turn; it never dispatches a request.
"""
from __future__ import annotations

import json
import hashlib
from collections import deque
import time
from dataclasses import asdict, fields
from typing import Any
from pathlib import Path

from hlt_provider_recovery import recovery_from_result

MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
_ITERATION_FIELDS = frozenset({
    "request_logger", "api_messages", "tools_for_api", "_moa_prepared_request",
    "approx_tokens", "request_pressure_tokens", "total_chars", "thinking_spinner",
    "api_start_time", "retry_count", "max_retries", "_retry", "finish_reason",
    "response", "api_kwargs", "api_request_id", "_original_api_kwargs",
    "_llm_middleware_trace", "api_duration", "assistant_message",
})
_AGENT_FIELDS = (
    "_current_turn_timestamp", "_run_budget_started_at", "_user_turn_count",
    "_turns_since_memory", "_iters_since_skill", "_budget_grace_call",
    "_iteration_budget_warning_injected", "_run_budget_wrapup_injected",
    "_verification_stop_nudges", "_pre_verify_nudges", "_last_turn_usage",
    "_invalid_tool_retries", "_invalid_json_retries", "_empty_content_retries",
    "_incomplete_scratchpad_retries", "_codex_incomplete_retries",
    "_codex_reasoning_only_streak", "_thinking_prefill_retries",
    "_post_tool_empty_retried", "_unicode_sanitization_passes",
    "_ephemeral_reasoning_off", "_auth_pool_refresh_counts", "_last_content_with_tools", "_last_content_tools_all_housekeeping", "_mute_post_response",
    "_turn_failed_file_mutations", "session_input_tokens", "session_output_tokens",
    "session_cache_read_tokens", "session_cache_write_tokens", "session_prompt_tokens",
    "session_completion_tokens", "session_total_tokens", "session_api_calls",
    "session_reasoning_tokens", "session_estimated_cost_usd", "session_cost_status", "session_cost_source",
)




def _runtime_contract_digest() -> str:
    # Exact owned source compatibility: an upgrade/rollback must never reinterpret
    # a parked private checkpoint merely because its JSON happens to deserialize.
    import agent.conversation_loop as native_loop
    native = Path(native_loop.__file__).resolve().parent
    local = Path(__file__).resolve().parent
    paths = [native / name for name in (
        'conversation_loop.py', 'turn_context.py', 'tool_guardrails.py', 'turn_tool_round.py',
        'turn_api_error.py', 'turn_recovery.py', 'turn_finalizer.py', 'tool_executor.py',
        'tool_dispatch_helpers.py', 'display.py')]
    paths += [native.parent / 'gateway' / 'platforms' / name for name in (
        'api_server_runs.py', 'api_server_run_idempotency.py')]
    paths += [native.parent / name for name in (
        'gateway/run_turn.py', 'gateway/run_turn_runner.py', 'gateway/run_managed_slack.py',
        'gateway/run_agent_cache.py', 'gateway/run_startup.py', 'gateway/run_shutdown.py',
        'gateway/run_busy.py', 'gateway/platforms/base.py',
        'gateway/stream_consumer.py', 'plugins/platforms/slack/adapter.py')]
    paths += [local / name for name in ('hlt_provider_checkpoint.py', 'hlt_artifact_run_context.py', 'hlt_numeric_grounding.py', 'fleet_run_budget.py', 'hlt_slack_provider_wait.py')]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def _route_contract_digest(agent: Any) -> str:
    # Refreshing/rotating credentials is expected recovery, not a new policy.
    # Hash only route/tool semantics; never persist credential values.
    keys = ('provider', 'model', 'base_url', 'api_mode', 'reasoning_config')
    primary = getattr(agent, '_primary_runtime', None) or {
        key: getattr(agent, key, None) for key in keys}
    def json_default(value):
        if isinstance(value, (set, frozenset)):
            return sorted(value)
        raise TypeError("unsupported route policy value")
    policy = {
        'guardrails': asdict(agent._tool_guardrails.config),
        'primary': {key: primary.get(key) for key in keys},
        'fallbacks': [{key: entry.get(key) for key in keys}
                      for entry in (getattr(agent, '_fallback_chain', None) or [])],
        'tools': getattr(agent, 'tools', None),
    }
    return hashlib.sha256(json.dumps(policy, sort_keys=True, allow_nan=False, default=json_default).encode()).hexdigest()



def _governance_digest() -> str:
    # The owning pack writer updates identity and permissions under this lock.
    # A parked turn may keep its task context, but must not restore superseded
    # authorization text after an operator changes the active pack.
    import fcntl
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    digest = hashlib.sha256()
    with (home / '.hlt-k2-runtime-pack.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        try:
            for path in (home / 'SOUL.md', home / 'grounding' / 'AGENTS.md'):
                digest.update(path.name.encode() + b'\0')
                digest.update(path.read_bytes() if path.exists() else b'absent')
                digest.update(b'\0')
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return digest.hexdigest()


def _guard_encode(value):
    from agent.tool_guardrails import ToolCallSignature, ToolGuardrailDecision
    if isinstance(value, (ToolCallSignature, ToolGuardrailDecision)):
        return {"type": type(value).__name__, "fields": {f.name: _guard_encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, dict):
        return {"type": "dict", "items": [[_guard_encode(k), _guard_encode(v)] for k, v in value.items()]}
    if isinstance(value, (tuple, list, deque)):
        return {"type": type(value).__name__, "items": [_guard_encode(v) for v in value],
                "maxlen": value.maxlen if isinstance(value, deque) else None}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("unsupported guardrail state")


def _guard_decode(value):
    from agent.tool_guardrails import ToolCallSignature, ToolGuardrailDecision
    if not isinstance(value, dict):
        return value
    kind = value["type"]
    if kind in {"ToolCallSignature", "ToolGuardrailDecision"}:
        cls = ToolCallSignature if kind == "ToolCallSignature" else ToolGuardrailDecision
        return cls(**{k: _guard_decode(v) for k, v in value["fields"].items()})
    items = value["items"]
    if kind == "dict":
        return {_guard_decode(k): _guard_decode(v) for k, v in items}
    items = [_guard_decode(v) for v in items]
    if kind == "deque":
        return deque(items, maxlen=value["maxlen"])
    if kind == "tuple":
        return tuple(items)
    if kind == "list":
        return items
    raise ValueError("unsupported guardrail state")


def _transcript_digest(agent):
    # Detect another writer, rewind or compression while the run was parked.
    rows = agent._session_db.get_messages(agent.session_id, include_inactive=True, include_compacted=True)
    return hashlib.sha256(json.dumps(rows, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _settled_tools(messages: list[dict], start: int) -> bool:
    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages[start:]:
        for call in message.get("tool_calls") or []:
            identifier = call.get("id") if isinstance(call, dict) else None
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                return False
            function = call.get("function") or {}
            # Detached children/processes have their own effect custody. Until
            # those owners supply a durable join receipt, do not auto-resume.
            if (function.get("name") in {"delegate_task", "execute_code", "process", "process_manage"}
                    or str(function.get("name") or "").startswith(("browser_", "computer_"))):
                return False
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    return False
            if isinstance(arguments, dict) and arguments.get("background"):
                return False
            pending.add(identifier)
            seen.add(identifier)
        if message.get("role") == "tool":
            identifier = message.get("tool_call_id")
            if identifier not in pending or message.get("is_error"):
                return False
            # Native executor/replay owners persist this separately from text.
            # A timed-out write may still be running even when its result is
            # plain prose; only an absent disposition or explicit no-effect
            # receipt can cross this boundary. Unknown future values fail closed.
            disposition = message.get("effect_disposition")
            if disposition is not None and disposition != "none":
                return False
            name = message.get('tool_name') or message.get('name') or ''
            content = message.get("content")
            if isinstance(content, str):
                # Hermes wraps external text before persisting it. Inspect the
                # original structured receipt inside that native wrapper too;
                # otherwise a failed/unknown effect looks like harmless prose.
                wrapped = content.startswith('<untrusted_tool_result ')
                if wrapped:
                    from agent.tool_dispatch_helpers import _maybe_wrap_untrusted
                    _, separator, remainder = content.partition('\n\n')
                    if not separator or not remainder.endswith('\n</untrusted_tool_result>'):
                        return False
                    original = remainder.removesuffix('\n</untrusted_tool_result>')
                    if not isinstance(name, str) or _maybe_wrap_untrusted(name, original) != content:
                        return False
                    content = original
                try:
                    content = json.loads(content)
                except (ValueError, TypeError):
                    if wrapped:
                        return False
            # Use the pinned executor's own classifier for native plain-text
            # failures (e.g. a worker that returned no result), after unwrapping
            # and decoding structured receipts rather than scanning quoted data.
            from agent.display import _detect_tool_failure
            if _detect_tool_failure(name, content)[0]:
                return False
            # A failed/uncertain tool effect needs reconciliation before any
            # automatic model continuation can decide to repeat that action.
            if isinstance(content, dict) and (
                content.get("error") or content.get("isError") or content.get("success") is False
                or content.get("status") in {"unknown", "uncertain", "failed", "timeout", "pending", "queued", "running", "in_progress"}
            ):
                return False
            pending.remove(identifier)
    return not pending


def snapshot_provider_wait(agent: Any, state: Any, result: dict) -> dict | None:
    recovery = recovery_from_result(result)
    if recovery is None or not getattr(agent, "_hlt_provider_wait_enabled", False):
        return None
    if (
        agent._interrupt_requested or state.moa_config is not None
        or getattr(agent, "_incremental_persistence_failed", False)
        or getattr(agent, "_hlt_scheduled_budget", None) is not None
        or state.pending_moa_prepared_request is not None
    ):
        return None
    messages = state.messages
    start = state.current_turn_user_idx
    if (
        not isinstance(messages, list) or not all(isinstance(row, dict) for row in messages)
        or not isinstance(start, int) or not 0 <= start < len(messages)
        or messages[start].get("role") != "user" or not _settled_tools(messages, start)
        or not getattr(agent, "_session_db", None)
    ):
        return None
    if agent._flush_messages_to_session_db(messages, state.conversation_history) is False:
        return None
    loop = {
        field.name: getattr(state, field.name)
        for field in fields(state) if field.name not in _ITERATION_FIELDS
    }
    checkpoint = {
        "schema": "hlt_provider_wait.v1",
        "runtime_contract_digest": _runtime_contract_digest(),
        "route_contract_digest": _route_contract_digest(agent),
        "governance_digest": _governance_digest(),
        "transcript_digest": _transcript_digest(agent),
        "guardrails": _guard_encode({k: v for k, v in vars(agent._tool_guardrails).items() if k != "config"}),
        "session_id": agent.session_id,
        "loop": loop,
        "agent": {name: getattr(agent, name, None) for name in _AGENT_FIELDS},
        "iteration_budget": {"max_total": agent.iteration_budget.max_total, "used": agent.iteration_budget.used},
        "max_iterations": agent.max_iterations,
        "delivered_interim_texts": sorted(getattr(agent, "_delivered_interim_texts", set())),
        "turn_file_mutation_paths": sorted(getattr(agent, "_turn_file_mutation_paths", set())),
    }
    try:
        encoded = json.dumps(checkpoint, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(encoded.encode()) > MAX_CHECKPOINT_BYTES:
        return None
    retry_at = recovery.get("retryAt")
    if retry_at is None or retry_at <= time.time() * 1000:
        retry_at = int(time.time() * 1000) + (900_000 if recovery["reason"] == "billing" else 300_000)
    recovery.update(retryAt=retry_at, automaticResume=True, continuationRequired=False)
    return {
        "provider_wait": True, "checkpoint": json.loads(encoded), "recovery": recovery,
        "completed": False, "messages": messages, "api_calls": state.api_call_count,
    }


def restore_turn_context(agent: Any, checkpoint: dict, stream_callback: Any, ra: Any) -> Any:
    from agent.turn_context import TurnContext, _bind_interrupt_scope, _reset_per_turn_agent_state, _hydrate_from_history
    from agent.iteration_budget import IterationBudget
    from agent.agent_runtime_helpers import note_turn_start

    if checkpoint.get("schema") != "hlt_provider_wait.v1" or checkpoint["session_id"] != agent.session_id:
        raise ValueError("provider continuation belongs to another session")
    if checkpoint.get("runtime_contract_digest") != _runtime_contract_digest():
        raise ValueError("provider continuation runtime changed; reconciliation required")
    if checkpoint.get("route_contract_digest") != _route_contract_digest(agent):
        raise ValueError("provider continuation model or tool policy changed; reconciliation required")
    if checkpoint.get("governance_digest") != _governance_digest():
        raise ValueError("provider continuation governance changed; reconciliation required")
    loop = checkpoint["loop"]
    if not isinstance(loop, dict) or not isinstance(loop.get("messages"), list):
        raise ValueError("invalid provider continuation")
    start = loop.get("current_turn_user_idx")
    if isinstance(start, bool) or not isinstance(start, int) or not 0 <= start < len(loop["messages"]):
        raise ValueError("invalid provider continuation user boundary")
    if loop["messages"][start].get("role") != "user":
        raise ValueError("invalid provider continuation user boundary")
    if not all(isinstance(loop.get(name), str) and loop[name] for name in ("effective_task_id", "turn_id")):
        raise ValueError("invalid provider continuation identity")
    if not _settled_tools(loop["messages"], start):
        raise ValueError("provider continuation contains unreconciled tool effects")
    if not agent._session_db or checkpoint["transcript_digest"] != _transcript_digest(agent):
        raise ValueError("provider continuation transcript changed; reconciliation required")
    budget = checkpoint["iteration_budget"]
    ceilings = (checkpoint["max_iterations"], budget["max_total"], budget["used"])
    if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100000 for v in ceilings):
        raise ValueError("invalid provider continuation budget")
    if budget["used"] > budget["max_total"] or budget["max_total"] < 1:
        raise ValueError("invalid provider continuation budget")
    _hydrate_from_history(agent, loop["messages"])
    _reset_per_turn_agent_state(agent)
    guardrails = _guard_decode(checkpoint["guardrails"])
    if set(guardrails) != set(vars(agent._tool_guardrails)) - {"config"}:
        raise ValueError("provider continuation guardrail version changed")
    for name, value in guardrails.items():
        setattr(agent._tool_guardrails, name, value)
    agent._tool_guardrail_halt_decision = agent._tool_guardrails.halt_decision
    for name, value in checkpoint["agent"].items():
        if name in _AGENT_FIELDS:
            setattr(agent, name, value)
    agent.max_iterations = min(agent.max_iterations, checkpoint["max_iterations"])
    budget = checkpoint["iteration_budget"]
    agent.iteration_budget = IterationBudget(min(agent.max_iterations, budget["max_total"]))
    for _ in range(budget["used"]):
        agent.iteration_budget.consume()
    agent._ensure_db_session()
    if not agent._session_db_created:
        raise ValueError("provider continuation session is unavailable")
    if agent._run_budget_started_at is not None:
        agent._run_budget_started_at = time.time() - getattr(agent, "_hlt_active_seconds", 0)
    agent._stream_callback = stream_callback
    agent._current_task_id, agent._current_turn_id = loop["effective_task_id"], loop["turn_id"]
    agent._current_api_request_id = ""
    agent._persist_user_message_idx = loop["current_turn_user_idx"]
    agent._persist_user_message_override = loop["original_user_message"]
    agent._cached_system_prompt = loop["active_system_prompt"]
    agent._session_messages = loop["messages"]
    agent._delivered_interim_texts = set(checkpoint["delivered_interim_texts"])
    agent._turn_file_mutation_paths = set(checkpoint["turn_file_mutation_paths"])
    agent._is_user_initiated_turn = False
    agent._hlt_restore_loop = loop
    note_turn_start(agent, loop["turn_id"])
    _bind_interrupt_scope(agent, ra)
    return TurnContext(**{
        field.name: loop.get(field.name, loop.get("_" + field.name))
        for field in fields(TurnContext)
    })
