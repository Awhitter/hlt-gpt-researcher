#!/usr/bin/env python3
"""Fail the image build if Hermes can lose a request or leak failover chatter."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any, Optional


def _require(source: str, needle: str, *, label: str) -> None:
    if needle not in source:
        raise AssertionError(f"missing {label}: {needle}")


def main(root: str) -> None:
    hermes = Path(root)
    loop_path = hermes / "agent" / "conversation_loop.py"
    gateway_path = hermes / "gateway" / "run.py"
    slack_path = hermes / "plugins" / "platforms" / "slack" / "adapter.py"

    loop = loop_path.read_text(encoding="utf-8")
    gateway = gateway_path.read_text(encoding="utf-8")
    slack = slack_path.read_text(encoding="utf-8")
    parsed: dict[Path, ast.Module] = {}
    for path, source in (
        (loop_path, loop),
        (gateway_path, gateway),
        (slack_path, slack),
    ):
        parsed[path] = ast.parse(source, filename=str(path))

    # Native split owners retain the authored request separately from retry messages.
    context = (hermes / "agent/turn_context.py").read_text()
    request = (hermes / "agent/turn_api_request.py").read_text()
    prep = (hermes / "agent/turn_iteration_prep.py").read_text()
    _require(context, "original_user_message = persist_user_message if persist_user_message is not None else user_message", label="original request")
    _require(loop, "original_user_message=s.original_user_message", label="loop request identity")
    _require(request, "user_message=original_user_message", label="request hook original input")
    _require(prep, "if _retry.restart_with_rebuilt_messages:", label="provider retry restart")
    _require(prep, "_retry.restart_with_rebuilt_messages = False", label="bounded restart")

    # A bare ownership transfer must recover the bounded full thread before the
    # gateway plugin promotes it into the durable user-message slot.
    _require(slack, "bare_agent_transfer", label="bare transfer detection")
    _require(
        slack,
        'after_ts="" if bare_agent_transfer else self._get_thread_watermark',
        label="full thread recovery for bare transfer",
    )

    # Slack sees neither internal provider warnings nor a low-quality fallback
    # answer. If both reviewed routes fail, it receives one concise result that
    # explicitly confirms the request remains available for retry.
    _require(
        gateway,
        "if _is_slack_gateway_surface(platform):\n            return None",
        label="Slack failover-status suppression",
    )
    _require(
        gateway,
        "both model routes are",
        label="concise two-route degradation",
    )
    _require(
        gateway,
        "Your request is preserved in this thread",
        label="preserved-request degradation receipt",
    )
    for warning in (
        "model\\s+fallback:",
        "primary\\s+model\\s+restored:",
        "switched\\s+to\\s+fallback:",
    ):
        _require(gateway, warning, label="provider warning filter")

    # Execute the small patched seam in isolation. This makes the image build
    # prove behavior rather than only matching source text, without importing
    # the full gateway or starting any provider/network work.
    gateway_tree = parsed[gateway_path]
    selected_nodes: list[ast.stmt] = []
    wanted_functions = {
        "_is_slack_gateway_surface",
        "_gateway_provider_error_reply",
        "_prepare_gateway_status_message",
    }
    for node in gateway_tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name)
                and target.id == "_TELEGRAM_NOISY_STATUS_RE"
                for target in targets
            ):
                selected_nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in wanted_functions:
                selected_nodes.append(node)
    namespace: dict[str, Any] = {
        "Any": Any,
        "Optional": Optional,
        "re": re,
        "COMPACTION_DONE_STATUS": "context compaction complete",
        "COMPACTION_STATUS": "compacting context",
        "COMPACTION_HEARTBEAT_STATUS": "compacting context in progress",
        "_GATEWAY_AUTH_ERROR_RE": re.compile("authentication", re.I),
        "_GATEWAY_PROVIDER_POLICY_RE": re.compile("policy", re.I),
        "_GATEWAY_RATE_LIMIT_RE": re.compile("rate", re.I),
        "_GATEWAY_CONNECTION_ERROR_RE": re.compile("connection", re.I),
        "_gateway_surface_passes_raw_text": lambda _platform: False,
        "_redact_gateway_user_facing_secrets": lambda text: text,
        "_gateway_compression_progress_notices_enabled": lambda: False,
        "_COMPRESSION_PROGRESS_STATUS_RE": re.compile(r"a^"),
        "_looks_like_gateway_provider_error": lambda text: bool(
            re.search(r"provider authentication failed|api call failed", text, re.I)
        ),
    }
    exec(
        compile(
            ast.Module(body=selected_nodes, type_ignores=[]),
            str(gateway_path),
            "exec",
        ),
        namespace,
    )
    degraded = namespace["_gateway_provider_error_reply"](
        "Provider authentication failed: secret detail", platform="slack"
    )
    assert degraded == (
        "I couldn't complete this right now because both model routes are "
        "unavailable. Your request is preserved in this thread; please retry "
        "shortly."
    )
    assert (
        namespace["_prepare_gateway_status_message"](
            "slack", "lifecycle", "Model fallback: switching providers"
        )
        is None
    )
    assert (
        namespace["_prepare_gateway_status_message"](
            "slack", "lifecycle", "Provider authentication failed: secret detail"
        )
        is None
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: assert_provider_failover_request_contract.py HERMES_ROOT")
    main(sys.argv[1])
