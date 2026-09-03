"""Port-owner and supervisor for whichever HLT agent this container runs.

Render runs this as a **web service**: something has to bind ``$PORT`` or the
health check fails and the deploy is rolled back. Hermes' Slack gateway talks
to Slack over Socket Mode — an outbound WebSocket — and never binds a port, so
it cannot be the container's main process on its own.

So this module owns the port, boots the agent's config, supervises the gateway as
a child process, and reports what actually happened. ``/health`` answers from
observed state (is the child alive? did the config get written? which tools is
he allowed?) rather than from "an env var is set", so a green check means the
agent is really up and really constrained.
"""

from __future__ import annotations

import hmac
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import agent_run_ledger
import cron_seed
import grounding
import render_config
import uvicorn
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="[hlt-agent] %(levelname)s %(message)s")
logger = logging.getLogger("hlt-agent")

HERMES_HOME = Path(os.getenv("HERMES_HOME", "/data/hermes"))
GATEWAY_ENABLED = os.getenv("AGENT_ENABLE_GATEWAY", "0") == "1"

# ── Socket Mode transport markers ────────────────────────────────────────────
# Cleo answered nobody in Slack from 2026-08-09T03:14:58Z to 2026-08-16, and
# /health returned 200 the entire time: `running` only asks whether the child
# PROCESS is alive, and `slack_adapter_available` is an import check made once at
# boot. Neither can see a websocket. The adapter's own health check fired twice,
# called disconnect(), and reconnected onto an aiohttp session that was already
# closed — so the process supervised cron happily while two orphaned socket
# clients retried forever. Render never bounced her because the check passed.
#
# The adapter says all of this in its logs, so the supervisor reads them.
SOCKET_UP_MARKERS = (
    "Bolt app is running",
    "Socket Mode connected",
    "slack connected",
)
SOCKET_DOWN_MARKERS = (
    "Failed to connect",
    "Session is closed",
    "Socket Mode unhealthy",
    "Connector is closed",
)
# One dropped frame is normal; Slack rotates sockets and the adapter reconnects.
# A run of failures with no successful connect between them is the stuck state.
SOCKET_FAILURE_TOLERANCE = int(os.getenv("AGENT_SOCKET_FAILURE_TOLERANCE", "5"))

# Emitted only after a provider call succeeds and reports usage. This is
# stronger evidence than config or a pre-call "conversation turn" line: it
# captures the route that actually answered, including a Hermes fallback.
# Contract source: pinned Hermes ``agent/conversation_loop.py`` at
# HERMES_UPSTREAM_REF. If upstream changes the formatter this intentionally
# yields no observed route (missing proof) instead of guessing from a nearby
# pre-call log; the parser test and exact-SHA canary make that drift visible.
SUCCESSFUL_MODEL_ROUTE_RE = re.compile(
    r"API call #\d+: model=(?P<model>\S+) provider=(?P<provider>\S+)\s"
)

# Give up after this many crashes so a bad config surfaces in /health instead
# of hiding behind an endless restart loop.
MAX_RESTARTS = 5
BACKOFF_CAP_SECONDS = 60

# Hermes only attaches a stderr handler when the CLI is given -v/-q, and with no
# flag it prints WARNING and above. Every line describing whether the bot is
# actually working — "Connecting to slack...", the connected confirmation, the
# per-message dispatch, and an authorization denial — is INFO. Running without
# -v therefore produces a log stream that looks healthy for a bot that is
# connected to nothing, which is the exact failure this service exists to make
# visible. Default to -v; 0 restores upstream's quiet default and 2+ gives DEBUG.
GATEWAY_VERBOSITY = os.getenv("AGENT_GATEWAY_VERBOSITY", "1")


def gateway_command(verbosity: str = GATEWAY_VERBOSITY) -> list[str]:
    """The argv for the gateway child.

    ``run`` must be explicit. Bare ``hermes gateway`` takes the same code path
    (``_gateway_command_inner``: "if subcmd is None or subcmd == 'run'"), but
    -v/-q/--external-supervisor are declared on the ``run`` sub-subparser, so
    `hermes gateway -v` is an argparse error, not a verbose gateway.

    ``--external-supervisor`` tells Hermes a process manager owns this
    foreground gateway, which is exactly true here: an in-chat restart or
    update then exits back to us to be restarted, instead of spawning a
    detached replacement that would leave two dispatchers on one Slack app.
    """
    cmd = ["hermes", "gateway", "run", "--external-supervisor"]
    try:
        level = int(verbosity)
    except (TypeError, ValueError):
        level = 1
    if level > 0:
        # -v, -vv, -vvv — argparse counts the repeats.
        cmd.append("-" + "v" * min(level, 3))
    return cmd


app = FastAPI(title="HLT agent")


class GatewaySupervisor:
    """Runs ``hermes gateway`` as a child and remembers why it stopped."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._stop = threading.Event()
        self._restarts = 0
        self._last_exit_code: int | None = None
        self._gave_up_reason: str | None = None
        self._started_at: float | None = None
        # Socket Mode transport state, read off the child's own log stream. See
        # `_note_gateway_line` for why this is observed rather than asked.
        self._socket_connected_at: float | None = None
        self._socket_last_failure: str | None = None
        self._socket_last_failure_at: float | None = None
        self._socket_failures_since_connect = 0
        self._observed_provider: str | None = None
        self._observed_model: str | None = None
        self._observed_route_at: float | None = None
        self._supervision_started = False

    @property
    def cli_present(self) -> bool:
        return shutil.which("hermes") is not None

    @property
    def mcp_sdk_available(self) -> bool:
        """Whether Hermes can use MCP servers at all.

        `mcp` is an OPTIONAL upstream extra. Without it Hermes logs
        "mcp package not installed -- MCP tool support disabled" at DEBUG and
        carries on: every server still reads as mounted, every `mcp-<server>`
        toolset still appears granted, and the agent has NOT ONE of their
        tools. Cleo told a teammate her Linear and codegraph tools were "not
        actually available to me in this session" while /health showed four
        servers mounted with no missing tokens. She was right.
        """
        return importlib.util.find_spec("mcp") is not None

    @property
    def slack_adapter_available(self) -> bool:
        """Whether Hermes can actually build a Slack adapter.

        slack-bolt is an optional extra upstream. Without it the gateway starts,
        logs "No adapter available for slack", keeps running for cron, and never
        connects — so `running: True` on its own is not evidence the bot can
        hear anyone. This is the check that tells those two states apart.
        """
        return importlib.util.find_spec("slack_bolt") is not None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            now = time.time()
            return {
                "requested": GATEWAY_ENABLED,
                "running": running,
                "cli_present": self.cli_present,
                "slack_adapter_available": self.slack_adapter_available,
                "mcp_sdk_available": self.mcp_sdk_available,
                "restarts": self._restarts,
                "last_exit_code": self._last_exit_code,
                "uptime_seconds": (
                    round(now - self._started_at, 1)
                    if running and self._started_at
                    else None
                ),
                "stopped_reason": self._gave_up_reason,
                # None = not observed yet (boot grace). True/False = observed.
                "slack_socket_connected": self._socket_state(),
                "slack_socket_connected_seconds_ago": (
                    round(now - self._socket_connected_at, 1)
                    if self._socket_connected_at
                    else None
                ),
                "slack_socket_failures_since_connect": self._socket_failures_since_connect,
                "slack_socket_last_failure": self._socket_last_failure,
                "slack_socket_last_failure_seconds_ago": (
                    round(now - self._socket_last_failure_at, 1)
                    if self._socket_last_failure_at
                    else None
                ),
                "observed_model_route": (
                    {
                        "provider": self._observed_provider,
                        "model": self._observed_model,
                        "source": "successful_api_call",
                        "seconds_ago": round(now - self._observed_route_at, 1),
                    }
                    if self._observed_provider
                    and self._observed_model
                    and self._observed_route_at
                    else None
                ),
            }

    def _socket_state(self) -> bool | None:
        """Is the Slack websocket actually up? None until we have seen either marker.

        Not a probe. Slack's Socket Mode gives the process no "am I connected"
        API, and `auth.test` answers about the TOKEN, not the transport — it
        would have returned 200 for all seven days Cleo sat deaf. The adapter
        does say so in its own logs, so that is what this reads.
        """
        if self._socket_connected_at is None and self._socket_last_failure_at is None:
            return None
        if self._socket_connected_at is None:
            return False
        if self._socket_last_failure_at is None:
            return True
        # A reconnect storm always logs failures AFTER the last good connect.
        if self._socket_last_failure_at <= self._socket_connected_at:
            return True
        return self._socket_failures_since_connect < SOCKET_FAILURE_TOLERANCE

    def _note_gateway_line(self, line: str) -> None:
        """Update transport state from one line of the child's log stream."""
        route = SUCCESSFUL_MODEL_ROUTE_RE.search(line)
        if route:
            with self._lock:
                self._observed_model = route.group("model")
                self._observed_provider = route.group("provider")
                self._observed_route_at = time.time()
        if any(marker in line for marker in SOCKET_UP_MARKERS):
            with self._lock:
                self._socket_connected_at = time.time()
                self._socket_failures_since_connect = 0
                self._socket_last_failure = None
            return
        if any(marker in line for marker in SOCKET_DOWN_MARKERS):
            with self._lock:
                self._socket_last_failure_at = time.time()
                self._socket_last_failure = line.strip()[:300]
                self._socket_failures_since_connect += 1

    def _pump_output(self, proc: subprocess.Popen[str]) -> None:
        """Tee the child's log stream: through to our stdout, and past the watcher.

        Piping is the cost of observing the transport at all, so the pump exists
        to make sure Render's log stream is unchanged by it — every line still
        goes out, in order, and a crash in the watcher can never swallow one.
        """
        stream = proc.stdout
        if stream is None:
            return
        try:
            for line in stream:
                print(line, end="", flush=True)
                try:
                    self._note_gateway_line(line)
                except Exception:
                    logger.exception("socket-state watcher failed on a log line")
        except Exception:
            logger.exception("gateway log pump stopped")

    def start(self) -> None:
        if not GATEWAY_ENABLED:
            logger.info(
                "gateway disabled (AGENT_ENABLE_GATEWAY != 1) — serving health only"
            )
            return
        if not self.cli_present:
            self._gave_up_reason = (
                "hermes CLI not found on PATH; the image failed to install hermes-agent"
            )
            logger.error(self._gave_up_reason)
            return
        with self._lock:
            if self._supervision_started:
                return
            self._supervision_started = True
            self._gave_up_reason = None
        threading.Thread(
            target=self._supervise, name="hermes-gateway", daemon=True
        ).start()

    def block_start(self, reason: str) -> None:
        """Record a fail-closed boot gate without starting a crash loop."""
        with self._lock:
            self._gave_up_reason = reason
        logger.error(reason)

    def _supervise(self) -> None:
        while not self._stop.is_set():
            logger.info("starting hermes gateway (attempt %d)", self._restarts + 1)
            with self._lock:
                # Inherit stdout/stderr so Hermes' own logs land in Render's log
                # stream, which is where anyone debugging this will actually look.
                cmd = gateway_command()
                logger.info("gateway argv: %s", " ".join(cmd))
                # Piped, not inherited, so the transport watcher can read the
                # adapter's own connect/disconnect lines. `_pump_output` writes
                # every line straight back out, so Render's stream is unchanged.
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(HERMES_HOME),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self._started_at = time.time()
                self._socket_connected_at = None
                self._socket_last_failure = None
                self._socket_last_failure_at = None
                self._socket_failures_since_connect = 0
                proc = self._proc
            threading.Thread(
                target=self._pump_output,
                args=(proc,),
                name="hermes-gateway-logs",
                daemon=True,
            ).start()

            exit_code = proc.wait()
            if self._stop.is_set():
                return

            with self._lock:
                self._last_exit_code = exit_code
                self._restarts += 1
                restarts = self._restarts

            logger.error("hermes gateway exited with code %s", exit_code)
            if restarts >= MAX_RESTARTS:
                with self._lock:
                    self._gave_up_reason = (
                        f"gateway exited {restarts} times (last code {exit_code}); "
                        f"not restarting — see the logs above for the cause"
                    )
                    reason = self._gave_up_reason
                logger.error(reason)
                return

            delay = min(5 * restarts, BACKOFF_CAP_SECONDS)
            logger.info("restarting gateway in %ss", delay)
            self._stop.wait(delay)

    def shutdown(self, *_: Any) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


supervisor = GatewaySupervisor()
BOOT: dict[str, Any] = {}

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"
SLACK_BOTS_INFO_URL = "https://slack.com/api/bots.info"
HERMES_API_BASE_URL = "http://127.0.0.1:8642"
MAX_HOOK_MESSAGE_CHARS = 65_536
MAX_HOOK_TIMEOUT_SECONDS = 900
ACTIVATION_CONTRACT_VERSION = "agent_host_activation_readiness.v1"
SLACK_IDENTITY_CONTRACT_VERSION = "slack_agent_identity.v1"
SLACK_IDENTITY_RESPONSE_HEADERS = {"Cache-Control": "no-store"}
_RUN_LEDGER_LOCK = threading.Lock()
_RUN_LEDGER: agent_run_ledger.AgentRunLedger | None = None
_RUN_LEDGER_PATH: Path | None = None

# Use the protocol revision shipped in the same MCP SDK Hermes runs. A dated
# literal here can keep a custom health probe green after the actual client has
# moved to a different wire contract. The fallback matches pinned Hermes'
# conservative streamable-HTTP fallback for older SDK builds.
try:
    from mcp.types import LATEST_PROTOCOL_VERSION as MCP_PROTOCOL_VERSION
except (ImportError, AttributeError):  # pragma: no cover - current image has MCP
    MCP_PROTOCOL_VERSION = "2025-03-26"

# These are the scopes that make Cleo useful in the surfaces she promises:
# channel mentions, private/group threads, user resolution, and visible file
# delivery. Reactions, commands and assistant chrome are valuable but do not
# decide whether a normal product task can be completed.
CORE_SLACK_SCOPES = frozenset(
    {
        "app_mentions:read",
        "channels:history",
        "chat:write",
        "files:write",
        "groups:history",
        "im:history",
        "im:write",
        "mpim:history",
        "users:read",
    }
)


def _decode_mcp_response(raw: bytes) -> dict[str, Any]:
    """Decode either JSON or the single-event SSE shape used by MCP HTTP."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    if text.startswith("{"):
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    for line in text.splitlines():
        if line.startswith("data:"):
            value = json.loads(line[5:].strip())
            if isinstance(value, dict):
                return value
    raise ValueError("MCP response contained neither JSON nor an SSE data event")


def _mcp_post(
    url: str,
    token: str,
    payload: dict[str, Any],
    *,
    session_id: str = "",
    timeout: float = 8.0,
) -> tuple[dict[str, Any], str, dict[str, str]]:
    """One streamable-HTTP MCP request, returning only non-secret metadata."""
    import urllib.request

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        response_headers = {
            key.lower(): value for key, value in response.headers.items()
        }
    return (
        _decode_mcp_response(raw),
        response_headers.get("mcp-session-id", session_id),
        response_headers,
    )


def _mcp_tool_data(result: dict[str, Any]) -> dict[str, Any]:
    """Project structured tool output from either MCP result representation."""
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if not isinstance(content, list):
        return {}
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            value = json.loads(str(item.get("text") or "{}"))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


K2_RUNTIME_PACK_NAMES = ("agents.runtime_pack", "agents_runtime_pack")
K2_WELL_START_NAMES = ("katailyst.well.start", "katailyst_well_start")
K2_WELL_GET_NAMES = ("katailyst.well.get", "katailyst_well_get")
K2_WELL_NAMES = ("katailyst.well", "katailyst_well")
K2_HERMES_HOST_PROFILE: dict[str, Any] = {
    "version": "agent_host_profile.v1",
    "profile": "paperclip_hermes",
    "capabilities": ["conversational_shell", "mcp_client"],
    "hostRef": "internal_system:hlt-hermes",
}


class K2ReadinessError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _mcp_error_text(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if error:
        return str(error)[:240]
    result = payload.get("result")
    result = result if isinstance(result, Mapping) else {}
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, Mapping) and item.get("type") == "text":
                return str(item.get("text") or "")[:240]
    return "Katailyst2 rejected the probe"


def _raise_for_rpc_error(payload: Mapping[str, Any], *, operation: str) -> None:
    result = payload.get("result")
    result = result if isinstance(result, Mapping) else {}
    if not payload.get("error") and result.get("isError") is not True:
        return
    message = _mcp_error_text(payload)
    lowered = message.lower()
    outage_markers = (
        "timeout",
        "timed out",
        "temporarily unavailable",
        "service unavailable",
        "backend unavailable",
        "connection pool",
        "too many connections",
        "overloaded",
    )
    kind = (
        "outage"
        if any(marker in lowered for marker in outage_markers)
        else "contract_rejected"
    )
    raise K2ReadinessError(kind, f"{operation}: {message}")


def _exception_kind(exc: Exception) -> str:
    if isinstance(exc, K2ReadinessError):
        return exc.kind
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in {408, 425, 429} or exc.code >= 500:
            return "outage"
        if exc.code in {401, 403}:
            return "auth_failed"
        return "contract_rejected"
    if isinstance(exc, urllib.error.URLError):
        return "outage"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "outage"
    return "contract_rejected"


def k2_agent_readiness(
    url: str,
    token: str,
    expected_agent_ref: str,
    runtime_lane: str = "hermes",
    timeout: float = 8.0,
    *,
    require_active: bool = True,
    probe_well: bool = True,
) -> dict[str, Any]:
    """Boot the canonical agent brain and prove the per-turn context door.

    Identity comes only from ``agents.runtime_pack``. The call deliberately
    omits ``agentRef``: returning Cleo's exact pack therefore proves the bearer
    is agent-bound rather than merely able to read a public catalog. The well
    is probed separately as task-time capability, never as identity.
    """
    started = time.monotonic()
    deadline = started + max(0.5, timeout)
    result: dict[str, Any] = {
        "mounted": bool(url),
        "bearer_present": bool(token),
        "transport_ok": None,
        "server_repo": "",
        "server_matches_katailyst2": None,
        "visible_tools": None,
        "runtime_pack_tool_listed": False,
        "runtime_pack_callable": False,
        "well_tool_listed": False,
        "well_callable": False,
        "well_status": "not_checked",
        "well_mode": "not_checked",
        "well_outage_declared": False,
        "agent_block_found": False,
        "agent_bound_token": False,
        "host_profile_compatible": False,
        "runtime_pack_version": "",
        "agent_version": None,
        "shared_doctrine_refs": [],
        "shared_doctrine_body_chars": 0,
        "activation_status": "",
        "activation_online": None,
        "activation_ready": False,
        "shell_scopes": [],
        "outage_declared": False,
        "requested_agent_ref": expected_agent_ref,
        "runtime_lane": runtime_lane,
        "contract_status": "not_checked",
        "resolved_agent_ref": "",
        "identity_matches": None,
        "latency_ms": None,
        "error": "",
    }
    if not url:
        result["contract_status"] = "not_mounted"
        return result
    if not token:
        result["contract_status"] = "missing_bearer"
        return result

    request_id = 0

    def rpc(method: str, params: dict[str, Any], session_id: str = ""):
        nonlocal request_id
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Katailyst2 readiness deadline expired")
        request_id += 1
        return _mcp_post(
            url,
            token,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            session_id=session_id,
            timeout=max(0.05, remaining),
        )

    try:
        initialized, session_id, headers = rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "hlt-cleo-readiness", "version": "1.0.0"},
            },
        )
        _raise_for_rpc_error(initialized, operation="initialize")
        result["transport_ok"] = True
        result["server_repo"] = headers.get("x-katailyst-repo", "")
        result["server_matches_katailyst2"] = (
            result["server_repo"].strip().lower() == "katailyst2"
        )
        if result["server_matches_katailyst2"] is not True:
            result["contract_status"] = "wrong_server"
            return result

        listed, session_id, _ = rpc("tools/list", {}, session_id)
        _raise_for_rpc_error(listed, operation="tools/list")
        tools = (listed.get("result") or {}).get("tools") or []
        names = [
            str(tool.get("name") or "") for tool in tools if isinstance(tool, dict)
        ]
        result["visible_tools"] = len(names)
        pack_tool = next((name for name in names if name in K2_RUNTIME_PACK_NAMES), "")
        well_start_tool = next(
            (name for name in names if name in K2_WELL_START_NAMES), ""
        )
        well_get_tool = next(
            (name for name in names if name in K2_WELL_GET_NAMES), ""
        )
        well_sync_tool = next((name for name in names if name in K2_WELL_NAMES), "")
        async_well_listed = bool(well_start_tool and well_get_tool)
        result["runtime_pack_tool_listed"] = bool(pack_tool)
        result["well_tool_listed"] = async_well_listed or bool(well_sync_tool)
        if not pack_tool or not result["well_tool_listed"]:
            result["contract_status"] = "tool_surface_incomplete"
            return result
        if not expected_agent_ref:
            result["contract_status"] = "not_requested"
            return result

        pack_call, _, _ = rpc(
            "tools/call",
            {
                "name": pack_tool,
                "arguments": {
                    # Omission is the agent-binding proof. Passing agentRef here
                    # would let an unbound broad token impersonate readiness.
                    "hostProfile": dict(K2_HERMES_HOST_PROFILE),
                    "requireActive": require_active,
                },
            },
            session_id,
        )
        _raise_for_rpc_error(pack_call, operation="agents.runtime_pack")
        pack_result = pack_call.get("result") or {}
        pack_data = _mcp_tool_data(pack_result)
        runtime_pack = pack_data.get("runtimePack")
        if not isinstance(runtime_pack, Mapping):
            raise K2ReadinessError(
                "contract_rejected", "agents.runtime_pack returned no runtimePack"
            )
        resolved_ref = str(runtime_pack.get("agentRef") or "")
        capability = runtime_pack.get("capability")
        capability = capability if isinstance(capability, Mapping) else {}
        activation = runtime_pack.get("activation")
        activation = activation if isinstance(activation, Mapping) else {}
        policies = runtime_pack.get("policies")
        policies = policies if isinstance(policies, Mapping) else {}
        shell_config = runtime_pack.get("shellConfig")
        shell_config = shell_config if isinstance(shell_config, Mapping) else {}
        shared_doctrine = shell_config.get("sharedDoctrine")
        shared_doctrine = (
            shared_doctrine if isinstance(shared_doctrine, list) else []
        )
        shared_doctrine_refs = [
            str(row.get("ref") or "")
            for row in shared_doctrine
            if isinstance(row, Mapping) and str(row.get("ref") or "").strip()
        ]
        shared_doctrine_body_chars = sum(
            len(str(row.get("body") or ""))
            for row in shared_doctrine
            if isinstance(row, Mapping)
        )
        shell_scopes = policies.get("shellScopes")
        shell_scopes = shell_scopes if isinstance(shell_scopes, list) else []
        identity_matches = resolved_ref == expected_agent_ref
        host_compatible = capability.get("compatible") is True
        token_scoped = "registry.read" in shell_scopes
        active = (
            activation.get("status") == "active" and activation.get("isOnline") is True
        )
        result.update(
            {
                "runtime_pack_callable": True,
                "agent_bound_token": identity_matches and token_scoped,
                "host_profile_compatible": host_compatible,
                "runtime_pack_version": str(runtime_pack.get("version") or ""),
                "agent_version": runtime_pack.get("agentVersion"),
                "shared_doctrine_refs": shared_doctrine_refs,
                "shared_doctrine_body_chars": shared_doctrine_body_chars,
                "activation_status": str(activation.get("status") or ""),
                "activation_online": activation.get("isOnline"),
                "activation_ready": active,
                "shell_scopes": [str(scope) for scope in shell_scopes],
                "agent_block_found": identity_matches,
                "resolved_agent_ref": resolved_ref,
                "identity_matches": identity_matches,
            }
        )
        if not identity_matches or not token_scoped or not host_compatible:
            result["contract_status"] = "runtime_pack_invalid"
            return result
        if require_active and not active:
            result["contract_status"] = "preactivation"
            return result
        if not active:
            # This exact state is the pre-activation handshake. It proves the
            # token is bound to Cleo and the pack resolves for Hermes without
            # pretending the agent is already online.
            result["contract_status"] = "preactivation"
            return result
        # Keep the canonical brain even if the independent task-context probe
        # reports a transient outage below. A working pack must never be
        # replaced by the bundled fallback merely because one well call failed.
        result["_runtime_pack"] = dict(runtime_pack)

        if not probe_well:
            result["contract_status"] = "pack_loaded"
            return result

        well_arguments = {
            "mission": "Show me one useful block for a Nursing Mastery product mission.",
            "facets": ["Nursing Mastery product work"],
            "budget": 1,
            "thoughts": False,
            "traverse": False,
        }
        if async_well_listed:
            start_call, _, _ = rpc(
                "tools/call",
                {
                    "name": well_start_tool,
                    "arguments": well_arguments,
                },
                session_id,
            )
            _raise_for_rpc_error(start_call, operation="katailyst.well.start")
            started_run = _mcp_tool_data(start_call.get("result") or {})
            run_id = str(started_run.get("runId") or "").strip()
            if not run_id:
                raise K2ReadinessError(
                    "contract_rejected", "katailyst.well.start returned no runId"
                )
            get_call, _, _ = rpc(
                "tools/call",
                {"name": well_get_tool, "arguments": {"runId": run_id}},
                session_id,
            )
            _raise_for_rpc_error(get_call, operation="katailyst.well.get")
            polled_run = _mcp_tool_data(get_call.get("result") or {})
            run_status = str(
                polled_run.get("status") or started_run.get("status") or "queued"
            ).strip().lower()
            if run_status in {"failed", "cancelled"}:
                raise K2ReadinessError(
                    "outage",
                    "katailyst.well async run "
                    f"{run_status}: {str(polled_run.get('error') or '')[:180]}",
                )
            result["well_mode"] = "async"
            result["well_status"] = run_status
        else:
            well_call, _, _ = rpc(
                "tools/call",
                {"name": well_sync_tool, "arguments": well_arguments},
                session_id,
            )
            _raise_for_rpc_error(well_call, operation="katailyst.well")
            result["well_mode"] = "sync_compat"
            result["well_status"] = "succeeded"
        result["well_callable"] = True
        result["contract_status"] = "loaded"
        return result
    except Exception as exc:
        if result["transport_ok"] is None:
            result["transport_ok"] = False
        kind = _exception_kind(exc)
        if "_runtime_pack" in result:
            # The canonical brain contract already succeeded. Keep the optional
            # task-context failure on its own status axis so /health does not
            # simultaneously claim both a healthy installed pack and a K2
            # contract outage.
            result["contract_status"] = "pack_loaded"
            result["outage_declared"] = False
            result["well_status"] = kind
            result["well_outage_declared"] = kind == "outage"
        else:
            result["contract_status"] = kind
            result["outage_declared"] = kind == "outage"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        return result
    finally:
        result["latency_ms"] = round((time.monotonic() - started) * 1000)


def _hook_token() -> str:
    return os.getenv("OPENCLAW_HQ_HOOK_TOKEN", "").strip()


def _hook_authorized(authorization: str | None) -> bool:
    expected = _hook_token()
    supplied = ""
    if isinstance(authorization, str) and authorization.startswith("Bearer "):
        supplied = authorization[7:].strip()
    return bool(expected) and hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )


def _agent_run_ledger_path() -> Path:
    configured = os.getenv("HLT_AGENT_RUN_LEDGER_PATH", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else HERMES_HOME / "agent-runs.sqlite3"
    )


def get_agent_run_ledger() -> agent_run_ledger.AgentRunLedger:
    global _RUN_LEDGER, _RUN_LEDGER_PATH
    path = _agent_run_ledger_path()
    with _RUN_LEDGER_LOCK:
        if _RUN_LEDGER is None or _RUN_LEDGER_PATH != path:
            _RUN_LEDGER = agent_run_ledger.AgentRunLedger(path)
            _RUN_LEDGER_PATH = path
        return _RUN_LEDGER


def agent_run_ledger_readiness() -> dict[str, Any]:
    try:
        result = get_agent_run_ledger().probe()
        return {
            "ready": result.get("ready") is True,
            "schema_version": result.get("schema_version"),
            "storage": "durable_sqlite",
            "error": "",
        }
    except Exception as exc:
        return {
            "ready": False,
            "schema_version": None,
            "storage": "durable_sqlite",
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }


def _validate_hook_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate K2's existing external-host envelope without widening it."""
    message = str(payload.get("message") or "").strip()
    if not message or len(message) > MAX_HOOK_MESSAGE_CHARS:
        raise ValueError(f"message must be 1..{MAX_HOOK_MESSAGE_CHARS} characters")
    agent_id = str(payload.get("agentId") or "").strip()
    expected_ref = str(BOOT.get("agent_ref") or "agent:cleo")
    if agent_id != expected_ref.removeprefix("agent:"):
        raise ValueError("agentId does not match this runtime")
    if payload.get("deliver") is not False:
        raise ValueError("deliver must be false for a K2 internal run")
    if payload.get("wakeMode") != "now":
        raise ValueError("wakeMode must be now")
    if payload.get("name") != "Katailyst2":
        raise ValueError("name must be Katailyst2")
    session_key = str(payload.get("sessionKey") or "").strip()
    if not re.fullmatch(r"hook:k2:[A-Za-z0-9._:-]{1,200}", session_key):
        raise ValueError("sessionKey must use the hook:k2:<run> namespace")
    timeout_seconds = payload.get("timeoutSeconds", 300)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise ValueError("timeoutSeconds must be an integer")
    if not 1 <= timeout_seconds <= MAX_HOOK_TIMEOUT_SECONDS:
        raise ValueError(f"timeoutSeconds must be 1..{MAX_HOOK_TIMEOUT_SECONDS}")
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be an object")
    if metadata.get("katailyst_agent_ref") != expected_ref:
        raise ValueError("metadata.katailyst_agent_ref does not match this runtime")
    k2_run_id = str(metadata.get("katailyst_run_id") or "").strip()
    if not k2_run_id:
        raise ValueError("metadata.katailyst_run_id is required")
    # This exact deterministic id is precomputed by K2 before POST and survives
    # a lost response. Reject non-canonical ids instead of letting two spellings
    # identify the same admission.
    agent_run_ledger.wrapper_run_id(k2_run_id)
    if session_key != f"hook:k2:{k2_run_id}":
        raise ValueError("sessionKey must exactly match metadata.katailyst_run_id")
    org_id = str(metadata.get("katailyst_org_id") or "").strip()
    if not org_id:
        raise ValueError("metadata.katailyst_org_id is required")
    if len(org_id) > 200:
        raise ValueError("metadata.katailyst_org_id is too long")
    return {
        "message": message,
        "session_key": session_key,
        "timeout_seconds": timeout_seconds,
        "agent_ref": expected_ref,
        "k2_run_id": k2_run_id,
        "org_id": org_id,
        "metadata": dict(metadata),
    }


def _hosted_k2_context_refs(metadata: Mapping[str, Any]) -> list[str]:
    """Read the refs K2 already selected without trusting arbitrary prompt text."""
    handoff = metadata.get("handoff")
    if not isinstance(handoff, Mapping):
        return []
    context = handoff.get("context")
    if not isinstance(context, Mapping):
        return []
    raw_refs = context.get("contextRefs")
    if not isinstance(raw_refs, list):
        return []
    refs: list[str] = []
    for value in raw_refs:
        ref = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,199}", ref):
            continue
        if ref not in refs:
            refs.append(ref)
        if len(refs) >= 12:
            break
    return refs


def _hosted_k2_run_instructions(normalized: Mapping[str, Any]) -> str:
    """Give bounded K2 missions an explicit finish-first operating contract.

    Hermes v0.21 does not accept a native per-run deadline field. The wrapper
    stops the run at ``timeoutSeconds``, so the agent must see that budget before
    its first model call and must not spend it rediscovering context K2 already
    supplied in the handoff.
    """
    timeout_seconds = int(normalized["timeout_seconds"])
    retrieval_seconds = timeout_seconds // 4
    desired_reserve_seconds = (
        max(8, timeout_seconds * 2 // 5)
        if timeout_seconds <= 30
        else min(60, max(15, timeout_seconds // 4))
    )
    reserve_seconds = min(max(0, timeout_seconds - 1), desired_reserve_seconds)
    final_by_seconds = max(1, timeout_seconds - reserve_seconds)
    refs = _hosted_k2_context_refs(normalized["metadata"])
    if refs:
        source_contract = (
            "K2 already selected these context refs: "
            + ", ".join(refs)
            + ". Use an exact ref directly with registry.get (or skill_content for "
            "an exact skill ref) when its body is needed. Do not call katailyst.well, "
            "registry.search, or Katailyst2 tool.search. If a supplied ref is "
            "unavailable, make at most one focused recovery search total."
        )
    else:
        source_contract = (
            "Use the mounted canonical runtime pack and the supplied K2 handoff first. "
            "Do not call katailyst.well or request a broad tool catalog. Make at most "
            "one focused discovery search total, and only when the answer genuinely "
            "depends on missing evidence."
        )
    return " ".join(
        (
            "This is a governed internal Katailyst2 mission for Cleo.",
            f"The hard end-to-end execution budget is {timeout_seconds} seconds; its clock starts before the first model call.",
            f"Spend no more than {retrieval_seconds} seconds (25% of the budget) on all retrieval and tool calls combined, and begin composing the final answer no later than {final_by_seconds} seconds after start, reserving {reserve_seconds} seconds to finish.",
            source_contract,
            "Return the best evidence-bounded answer before the deadline even when some evidence remains unavailable; never trade the requested final for more discovery.",
            "Complete the requested output shape compactly, preserve evidence, and do not send the result to Slack unless the mission itself explicitly requests a governed external effect.",
        )
    )


def _hermes_api_json(
    path: str,
    *,
    method: str = "GET",
    token: str = "",
    payload: Mapping[str, Any] | None = None,
    session_key: str = "",
    timeout: float = 6.0,
) -> tuple[int, dict[str, Any]]:
    import urllib.error
    import urllib.request

    headers = {"Accept": "application/json"}
    body: bytes | None = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_key:
        headers["X-Hermes-Session-Key"] = session_key
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(dict(payload)).encode("utf-8")
    request = urllib.request.Request(
        f"{HERMES_API_BASE_URL}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
    try:
        decoded = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except json.JSONDecodeError:
        decoded = {}
    return status, decoded if isinstance(decoded, dict) else {}


def hermes_api_readiness(timeout: float = 2.0) -> dict[str, Any]:
    result = {"reachable": False, "status": None, "error": ""}
    try:
        status, payload = _hermes_api_json("/health", timeout=timeout)
        result["status"] = status
        result["reachable"] = status == 200 and payload.get("status") == "ok"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return result


def _schedule_run_timeout(run_id: str, token: str, timeout_seconds: int) -> None:
    def stop() -> None:
        try:
            _hermes_api_json(
                f"/v1/runs/{run_id}/stop",
                method="POST",
                token=token,
                payload={},
                timeout=4.0,
            )
        except Exception:
            logger.warning("could not stop expired Hermes run %s", run_id)

    timer = threading.Timer(timeout_seconds, stop)
    timer.daemon = True
    timer.start()


def _provider_status(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "pending": "queued",
        "accepted": "queued",
        "started": "running",
        "in_progress": "running",
        "processing": "running",
        "succeeded": "completed",
        "success": "completed",
        "canceled": "cancelled",
        "aborted": "cancelled",
    }
    return aliases.get(normalized, normalized or "unknown")


def _admission_receipt(record: Mapping[str, Any]) -> dict[str, Any]:
    admission_status = str(record.get("admission_status") or "unknown")
    provider_status = _provider_status(record.get("provider_status"))
    if admission_status == "queued":
        public_status = "queued"
    elif admission_status == "dispatching":
        public_status = "unknown"
    else:
        public_status = provider_status
    terminal = admission_status == "terminal"
    body: dict[str, Any] = {
        "ok": public_status == "completed" if terminal else True,
        "runId": str(record.get("wrapper_run_id") or ""),
        "status": public_status,
        "terminal": terminal,
        "admissionStatus": admission_status,
        "statusUrl": f"/hooks/agent/runs/{record.get('wrapper_run_id')}",
    }
    recovery_code = str(record.get("recovery_code") or "")
    if admission_status == "dispatching" and not recovery_code:
        recovery_code = "provider_admission_ambiguous"
    if recovery_code and not terminal:
        body["recovery"] = {
            "code": recovery_code,
            "required": True,
        }
    if terminal and record.get("output_text"):
        body["output"] = str(record["output_text"])
    if record.get("error_text") and (terminal or recovery_code):
        body["error"] = str(record["error_text"])
    if terminal and record.get("usage") is not None:
        body["usage"] = record["usage"]
    return body


def _hook_admission_fingerprint(normalized: Mapping[str, Any]) -> str:
    return agent_run_ledger.request_fingerprint(
        {
            "message": normalized["message"],
            "sessionKey": normalized["session_key"],
            "timeoutSeconds": normalized["timeout_seconds"],
            "agentRef": normalized["agent_ref"],
            "metadata": normalized["metadata"],
        }
    )


def dispatch_agent_hook(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Admit exactly once, then cross the provider boundary at most once."""
    normalized = _validate_hook_payload(payload)
    ledger = get_agent_run_ledger()
    record, _created = ledger.admit(
        k2_run_id=normalized["k2_run_id"],
        session_key=normalized["session_key"],
        org_id=normalized["org_id"],
        agent_ref=normalized["agent_ref"],
        fingerprint=_hook_admission_fingerprint(normalized),
    )
    wrapper_id = str(record["wrapper_run_id"])

    # Only the process that atomically moves queued -> dispatching may call
    # Hermes. A replay of dispatching is the crash-ambiguous state and must not
    # speculate that a second POST is safe.
    if record.get("admission_status") != "queued" or not ledger.claim_dispatch(
        wrapper_id
    ):
        return _admission_receipt(ledger.get(wrapper_id) or record)

    token = _hook_token()
    try:
        status, response = _hermes_api_json(
            "/v1/runs",
            method="POST",
            token=token,
            session_key=normalized["session_key"],
            payload={
                "input": normalized["message"],
                "session_id": normalized["session_key"],
                "instructions": _hosted_k2_run_instructions(normalized),
            },
            timeout=8.0,
        )
    except Exception as exc:
        ledger.mark_dispatch_ambiguous(
            wrapper_id,
            f"provider admission response unavailable: {type(exc).__name__}: {exc}",
        )
        return _admission_receipt(ledger.get(wrapper_id) or record)

    provider_run_id = str(response.get("run_id") or "").strip()
    if status == 202 and re.fullmatch(r"run_[a-f0-9]{32}", provider_run_id):
        # This binding is the second durable fact. A crash before it leaves the
        # admission in dispatching/unknown forever rather than dispatching twice.
        ledger.bind_provider(wrapper_id, provider_run_id)
        try:
            _schedule_run_timeout(provider_run_id, token, normalized["timeout_seconds"])
        except Exception as exc:
            logger.warning(
                "could not schedule timeout for provider run %s: %s",
                provider_run_id,
                exc,
            )
        return _admission_receipt(ledger.get(wrapper_id) or record)

    error = response.get("error")
    if isinstance(error, Mapping):
        error = error.get("message") or error.get("code")
    detail = f"Hermes admission HTTP {status}: {error or 'invalid response'}"
    if 400 <= status < 500:
        # A concrete client rejection is the one safe proof that Hermes did not
        # accept this mission. Persist it as terminal so K2 can close the run.
        ledger.mark_terminal(wrapper_id, "failed", error=detail)
    else:
        # A 2xx with a malformed id or any server-side response can have crossed
        # the provider boundary. Never infer that retrying is safe.
        ledger.mark_dispatch_ambiguous(wrapper_id, detail)
    return _admission_receipt(ledger.get(wrapper_id) or record)


def read_agent_hook_run(run_id: str) -> tuple[int, dict[str, Any]]:
    if not re.fullmatch(r"run_[a-f0-9]{32}", run_id):
        return 400, {"ok": False, "error": "invalid runId"}
    ledger = get_agent_run_ledger()
    record = ledger.get(run_id)
    if record is None:
        return 404, {"ok": False, "runId": run_id, "error": "run not admitted"}
    if record.get("admission_status") != "provider_bound":
        return 200, _admission_receipt(record)

    provider_run_id = str(record.get("provider_run_id") or "")
    if not re.fullmatch(r"run_[a-f0-9]{32}", provider_run_id):
        ledger.note_provider_unknown(
            run_id,
            "provider_binding_invalid",
            "durable admission has no valid native Hermes run id",
        )
        return 200, _admission_receipt(ledger.get(run_id) or record)

    try:
        status, response = _hermes_api_json(
            f"/v1/runs/{provider_run_id}", token=_hook_token(), timeout=6.0
        )
    except Exception as exc:
        ledger.note_provider_unknown(
            run_id,
            "provider_status_unavailable",
            f"native Hermes status unavailable: {type(exc).__name__}: {exc}",
        )
        return 200, _admission_receipt(ledger.get(run_id) or record)

    returned_provider_id = str(response.get("run_id") or provider_run_id)
    if status != 200 or returned_provider_id != provider_run_id:
        ledger.note_provider_unknown(
            run_id,
            "provider_status_unavailable",
            f"native Hermes status HTTP {status} or mismatched provider id",
        )
        return 200, _admission_receipt(ledger.get(run_id) or record)

    provider_status = _provider_status(response.get("status"))
    if provider_status in {"completed", "failed", "cancelled"}:
        ledger.mark_terminal(
            run_id,
            provider_status,
            output=response.get("output"),
            error=response.get("error"),
            usage=response.get("usage"),
        )
    elif provider_status in {"queued", "running", "waiting_for_approval"}:
        ledger.note_provider_status(run_id, provider_status)
    else:
        ledger.note_provider_unknown(
            run_id,
            "provider_status_unknown",
            f"native Hermes returned unsupported status {provider_status!r}",
        )
    return 200, _admission_receipt(ledger.get(run_id) or record)


def openrouter_key_kind(key: str, timeout: float = 6.0) -> str:
    """Classify the model credential: can it actually run inference?

    OpenRouter issues two kinds of ``sk-or-v1-`` key that are indistinguishable
    by prefix, length or shape. A *provisioning* key authenticates fine and is
    then refused for every completion with 401 "User not found." — so "the env
    var is set" says nothing about whether the agent can answer anyone. Cleo
    shipped on one and replied to every message "Provider authentication
    failed", while /health reported ``openrouter_key_present: true``.

    ``/api/v1/key`` names the kind without spending a token, so this is one
    cheap call at boot rather than a live completion.

    Returns ``inference`` | ``provisioning`` | ``rejected`` | ``unknown``.
    ``unknown`` means the check itself could not run (offline, DNS, timeout)
    and is never treated as a failure — a flaky network must not make a
    working agent look broken.
    """
    if not key:
        return "unknown"
    import json as _json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = _json.loads(response.read()).get("data") or {}
    except urllib.error.HTTPError as exc:
        return "rejected" if exc.code in (401, 403) else "unknown"
    except Exception:
        return "unknown"

    if data.get("is_provisioning_key") or data.get("is_management_key"):
        return "provisioning"
    return "inference"


def subscription_auth_readiness(provider: str) -> dict[str, Any]:
    """Read OAuth-provider readiness without exposing stored tokens.

    Subscription credentials live in Hermes' persistent ``auth.json`` rather
    than Render environment variables. Checking only ``OPENROUTER_API_KEY``
    therefore mislabels a subscription-backed agent as credentialless and can
    miss an expired OAuth grant. Hermes owns token parsing and refresh state;
    this adapter copies only the non-secret status fields into ``/health``.
    """
    result: dict[str, Any] = {
        "provider": provider,
        "logged_in": None,
        "rate_limited": False,
        "reset_at": None,
        "last_refresh": None,
        "error": "",
    }
    try:
        if provider == "xai-oauth":
            from hermes_cli.auth import get_xai_oauth_auth_status

            status = get_xai_oauth_auth_status() or {}
        elif provider == "openai-codex":
            from hermes_cli.auth import get_codex_auth_status

            status = get_codex_auth_status() or {}
        else:
            return result
    except Exception as exc:
        # An import/read failure is unknown, not evidence that a working token
        # is bad. The gateway's real provider call remains the final authority.
        result["error"] = f"status check unavailable: {type(exc).__name__}"
        return result

    result["logged_in"] = bool(status.get("logged_in"))
    # Codex keeps a valid OAuth credential in the pool while its subscription
    # quota is exhausted. That is correctly "logged in", but it is not an
    # available recovery route. Copy only the non-secret cooldown fields;
    # never expose the api_key returned by Hermes' status helper.
    result["rate_limited"] = bool(status.get("rate_limited"))
    result["reset_at"] = status.get("reset_at")
    result["last_refresh"] = status.get("last_refresh")
    result["error"] = str(status.get("error") or "")
    return result


def model_route_readiness(
    routes: list[dict[str, str]], env: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Credential readback for every configured primary/fallback route."""
    provider_state: dict[str, dict[str, Any]] = {}
    for route in routes:
        provider = str(route.get("provider") or "").strip().lower()
        if not provider or provider in provider_state:
            continue
        if provider in {"xai-oauth", "openai-codex"}:
            auth = subscription_auth_readiness(provider)
            if auth.get("logged_in") is False or auth.get("rate_limited") is True:
                available: bool | None = False
            elif auth.get("logged_in") is True:
                available = True
            else:
                available = None
            provider_state[provider] = {
                "available": available,
                "credential": "subscription_oauth",
                "detail": auth,
            }
        elif provider == "openrouter":
            key = (env.get("OPENROUTER_API_KEY") or "").strip()
            kind = openrouter_key_kind(key) if key else "missing"
            if kind == "inference":
                available: bool | None = True
            elif kind in {"missing", "provisioning", "rejected"}:
                available = False
            else:
                available = None
            provider_state[provider] = {
                "available": available,
                "credential": "api_key",
                "detail": {"kind": kind},
            }
        else:
            provider_state[provider] = {
                "available": None,
                "credential": "unknown",
                "detail": {},
            }

    return [
        {
            **route,
            **provider_state.get(str(route.get("provider") or "").lower(), {}),
        }
        for route in routes
    ]


def web_search_readiness(backend: str, env: Mapping[str, str]) -> dict[str, Any]:
    """Prove the selected Hermes web backend can load before the first turn."""
    provider = backend.strip().lower()
    if provider == "firecrawl":
        credential_present = bool((env.get("FIRECRAWL_API_KEY") or "").strip())
        sdk_available = importlib.util.find_spec("firecrawl") is not None
        return {
            "provider": provider,
            "credential_present": credential_present,
            "sdk_available": sdk_available,
            "available": credential_present and sdk_available,
        }
    if provider == "ddgs":
        sdk_available = importlib.util.find_spec("ddgs") is not None
        return {
            "provider": provider,
            "credential_present": None,
            "sdk_available": sdk_available,
            "available": sdk_available,
        }
    return {
        "provider": provider,
        "credential_present": None,
        "sdk_available": None,
        "available": None,
    }


def slack_auth_readiness(token: str, timeout: float = 6.0) -> dict[str, Any]:
    """Verify the bot token and, when Slack reports them, its live scopes.

    A manifest or README proves only what we wanted to install. Slack returns
    the granted OAuth scopes on Web API responses, which is the readback that
    catches an app created from an old two-scope template. Some proxies omit
    that header; in that case auth can still be proven and scope state remains
    unknown rather than being mislabeled missing.
    """
    result: dict[str, Any] = {
        "configured": bool(token),
        "auth_ok": None,
        "scopes_known": False,
        "granted_scopes": [],
        "missing_core_scopes": [],
        "artifact_delivery_ready": None,
        "identity_ok": None,
        "identity": None,
    }
    if not token:
        return result

    import json as _json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        SLACK_AUTH_TEST_URL,
        data=b"",
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = _json.loads(response.read())
            raw_scopes = response.headers.get("x-oauth-scopes", "")
    except urllib.error.HTTPError as exc:
        result["auth_ok"] = False if exc.code in (401, 403) else None
        return result
    except Exception:
        return result

    result["auth_ok"] = bool(payload.get("ok"))
    if not result["auth_ok"]:
        return result

    if raw_scopes:
        scopes = sorted(
            {scope.strip() for scope in raw_scopes.split(",") if scope.strip()}
        )
        result["scopes_known"] = True
        result["granted_scopes"] = scopes
        result["missing_core_scopes"] = sorted(CORE_SLACK_SCOPES - set(scopes))
        result["artifact_delivery_ready"] = "files:write" in scopes

    workspace_id = str(payload.get("team_id") or "").strip()
    bot_id = str(payload.get("bot_id") or "").strip()
    bot_user_id = str(payload.get("user_id") or "").strip()
    if not workspace_id or not bot_id or not bot_user_id:
        result["identity_ok"] = False
        return result

    info_request = urllib.request.Request(
        SLACK_BOTS_INFO_URL,
        data=urllib.parse.urlencode({"bot": bot_id, "team_id": workspace_id}).encode(),
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(info_request, timeout=timeout) as response:
            info_payload = _json.loads(response.read())
    except urllib.error.HTTPError as exc:
        result["identity_ok"] = False if exc.code in (400, 401, 403) else None
        return result
    except Exception:
        return result

    bot = info_payload.get("bot") if isinstance(info_payload, dict) else None
    bot = bot if isinstance(bot, dict) else {}
    app_id = str(bot.get("app_id") or "").strip()
    info_bot_id = str(bot.get("id") or "").strip()
    info_user_id = str(bot.get("user_id") or "").strip()
    identity_ok = bool(
        info_payload.get("ok")
        and bot.get("deleted") is not True
        and app_id
        and info_bot_id == bot_id
        and info_user_id == bot_user_id
    )
    result["identity_ok"] = identity_ok
    if identity_ok:
        result["identity"] = {
            "workspaceId": workspace_id,
            "workspaceName": str(payload.get("team") or "").strip() or None,
            "appId": app_id,
            "botId": bot_id,
            "botUserId": bot_user_id,
            "botName": str(bot.get("name") or "").strip() or None,
            "verifiedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    return result


def _probe_k2_boot_contract(
    *, require_active: bool, probe_well: bool
) -> dict[str, Any]:
    return k2_agent_readiness(
        os.getenv("KATAILYST2_MCP_URL", "").strip(),
        os.getenv("KATAILYST2_MCP_TOKEN", "").strip(),
        str(BOOT.get("agent_ref") or ""),
        str(BOOT.get("runtime_lane") or "hermes"),
        require_active=require_active,
        probe_well=probe_well,
    )


def _publish_k2_readiness(k2_readiness: dict[str, Any]) -> dict[str, Any]:
    """The ONLY door onto BOOT["k2_agent_readiness"].

    The probe carries the full runtime pack (system prompt, doctrine, reference
    bodies) under `_runtime_pack` so a later step can install it. BOOT is
    published verbatim by the unauthenticated GET /health, so the pack must be
    stripped on EVERY publish — including the failure statuses, where nothing
    else would have popped it. Assigning BOOT["k2_agent_readiness"] anywhere
    else reopens the leak.
    """
    k2_readiness.pop("_runtime_pack", None)
    BOOT["k2_agent_readiness"] = k2_readiness
    return k2_readiness


def _install_active_k2_pack(k2_readiness: dict[str, Any]) -> bool:
    """Install one positively active pack and publish its safe boot receipt."""
    runtime_pack = k2_readiness.pop("_runtime_pack", None)
    if runtime_pack is None:
        _publish_k2_readiness(k2_readiness)
        return False
    pack_install = grounding.install_runtime_pack(
        runtime_pack,
        expected_agent_ref=str(BOOT.get("agent_ref") or ""),
        home=os.getenv("HERMES_HOME", str(HERMES_HOME)),
    )
    BOOT.update(pack_install)
    if pack_install.get("runtime_pack_applied") is not True:
        k2_readiness["contract_status"] = "runtime_pack_apply_failed"
        k2_readiness["error"] = str(
            pack_install.get("runtime_pack_error") or "runtime pack install failed"
        )[:240]
        k2_readiness["outage_declared"] = False
        _publish_k2_readiness(k2_readiness)
        return False
    _publish_k2_readiness(k2_readiness)
    return True


def _install_available_k2_pack(k2_readiness: dict[str, Any]) -> bool:
    """Install a verified pack even when the independent Well probe is down.

    ``k2_agent_readiness`` retains ``_runtime_pack`` after a later Well outage.
    The pack is the canonical identity and doctrine; the Well is task-time
    context. Losing the former because the latter exceeded its timeout makes a
    healthy agent boot from stale bundled grounding, which is strictly worse
    than starting the canonical brain with context readiness marked degraded.
    """
    if "_runtime_pack" not in k2_readiness:
        _publish_k2_readiness(k2_readiness)
        return False
    return _install_active_k2_pack(k2_readiness)


def _activation_poll_seconds() -> float:
    try:
        value = float(os.getenv("K2_ACTIVATION_POLL_SECONDS", "10"))
    except (TypeError, ValueError):
        value = 10.0
    return min(300.0, max(5.0, value))


def _try_k2_activation_once() -> bool:
    """Promote one newly active K2 pack; return whether Hermes may start."""
    preactivation = _probe_k2_boot_contract(
        require_active=False,
        probe_well=False,
    )
    _publish_k2_readiness(preactivation)
    if preactivation.get("activation_ready") is not True:
        return False
    active = _probe_k2_boot_contract(require_active=True, probe_well=True)
    if not _install_available_k2_pack(active):
        return False
    plugin = BOOT.get("k2_context_plugin") or {}
    slack_lead = BOOT.get("slack_agent_lead") or {}
    slack_lead_ready = slack_lead.get("local_agent_ready") is True and (
        slack_lead.get("required") is not True
        or slack_lead.get("roster_ready") is True
    )
    BOOT["gateway_start_allowed"] = (
        plugin.get("installed") is True
        and plugin.get("enabled") is True
        and slack_lead_ready
    )
    return BOOT["gateway_start_allowed"]


def _watch_for_k2_activation() -> None:
    """Bridge K2's offline-to-online transition without a manual redeploy.

    The authenticated pre-activation probe lets K2 verify this hosted body
    before it marks the agent online. Once that happens, this watcher repeats
    the real ``requireActive:true`` read, installs the canonical pack, proves
    the well, and only then starts Hermes. It never promotes from a health
    response.
    """
    while not supervisor._stop.wait(_activation_poll_seconds()):
        if _try_k2_activation_once():
            logger.info("Katailyst2 activated Cleo; starting the Hermes gateway")
            supervisor.start()
            return


def boot() -> None:
    BOOT.clear()
    BOOT.update(grounding.install())
    hook_token = os.getenv("OPENCLAW_HQ_HOOK_TOKEN", "").strip()
    if hook_token:
        # Hermes' loopback API uses the same credential as K2's public hook.
        # Keep one secret and one rotation boundary; never write its value to
        # config or health output.
        os.environ["API_SERVER_KEY"] = hook_token
    BOOT.update(render_config.render())
    BOOT["agent_run_ledger"] = (
        agent_run_ledger_readiness()
        if hook_token
        else {
            "ready": False,
            "schema_version": None,
            "storage": "durable_sqlite",
            "error": "hook not configured",
        }
    )
    for key, value in BOOT.items():
        logger.info("config %s: %s", key, value)
    active_provider = BOOT.get("model_provider") or ""
    if active_provider in {"xai-oauth", "openai-codex"}:
        subscription_auth = subscription_auth_readiness(active_provider)
        BOOT["subscription_auth"] = subscription_auth
        logger.info("config subscription_auth: %s", subscription_auth)
        if subscription_auth["logged_in"] is False:
            logger.error(
                "%s is the active model provider but its subscription OAuth "
                "credential is not logged in",
                active_provider,
            )
    elif active_provider == "openrouter" and not BOOT.get("openrouter_key_present"):
        logger.warning(
            "OPENROUTER_API_KEY is not set — the active provider has no credentials"
        )
    elif active_provider == "openrouter":
        kind = openrouter_key_kind(os.getenv("OPENROUTER_API_KEY", ""))
        BOOT["openrouter_key_kind"] = kind
        logger.info("config openrouter_key_kind: %s", kind)
        if kind == "provisioning":
            logger.error(
                "OPENROUTER_API_KEY is a PROVISIONING key, not an inference key. "
                "It authenticates to OpenRouter but every completion is refused "
                "401 'User not found.', so the agent will answer every message "
                "with 'Provider authentication failed'."
            )
        elif kind == "rejected":
            logger.error("OPENROUTER_API_KEY was rejected by OpenRouter")
    configured_routes = BOOT.get("configured_model_route") or []
    BOOT["model_route_readiness"] = model_route_readiness(configured_routes, os.environ)
    logger.info("config model_route_readiness: %s", BOOT["model_route_readiness"])
    BOOT["web_search_readiness"] = web_search_readiness(
        str(BOOT.get("web_search_backend") or ""), os.environ
    )
    logger.info("config web_search_readiness: %s", BOOT["web_search_readiness"])

    preactivation = _probe_k2_boot_contract(
        require_active=False,
        probe_well=False,
    )
    if preactivation.get("activation_ready") is True:
        # The actual boot contract is deliberately repeated with
        # requireActive:true. The preflight call above exists only to break the
        # offline activation circle; it never authorizes an active runtime.
        k2_readiness = _probe_k2_boot_contract(
            require_active=True,
            probe_well=True,
        )
    else:
        k2_readiness = preactivation

    if not _install_available_k2_pack(k2_readiness) and (
        BOOT.get("agent_ref") and k2_readiness.get("outage_declared") is True
    ):
        BOOT["brain_source"] = "bundled_outage_fallback"
        BOOT["bundled_fallback_reason"] = k2_readiness.get("error") or "K2 outage"

    plugin_installed = "hlt_k2_context" in (BOOT.get("plugins_installed") or [])
    plugin_enabled = (BOOT.get("k2_context_plugin") or {}).get(
        "enabled"
    ) is True and not BOOT.get("preserved_operator_config")
    BOOT["k2_context_plugin"] = {
        "installed": plugin_installed,
        "enabled": plugin_enabled,
        "hook": "pre_llm_call",
        "hooks": ["pre_gateway_dispatch", "pre_llm_call"],
    }
    slack_lead = BOOT.get("slack_agent_lead") or {}
    slack_lead_ready = slack_lead.get("local_agent_ready") is True and (
        slack_lead.get("required") is not True
        or slack_lead.get("roster_ready") is True
    )
    brain_ready = (
        not BOOT.get("agent_ref")
        or BOOT.get("runtime_pack_applied") is True
        or k2_readiness.get("outage_declared") is True
    )
    BOOT["gateway_start_allowed"] = bool(
        brain_ready and plugin_installed and plugin_enabled and slack_lead_ready
    )
    _publish_k2_readiness(k2_readiness)
    logger.info("config k2_agent_readiness: %s", k2_readiness)
    if (
        BOOT.get("agent_ref")
        and BOOT.get("runtime_pack_applied") is True
        and k2_readiness.get("well_callable") is not True
    ):
        logger.warning(
            "Katailyst2 loaded the canonical %s runtime pack, but optional "
            "Wishing Well enrichment was unavailable at boot",
            BOOT.get("agent_ref"),
        )
    elif BOOT.get("agent_ref") and k2_readiness["contract_status"] != "loaded":
        logger.error(
            "Katailyst2 did not load %s (status=%s)",
            BOOT.get("agent_ref") or "the configured agent",
            k2_readiness["contract_status"],
        )
    elif k2_readiness.get("identity_matches") is False:
        logger.error(
            "Katailyst2 resolved %s instead of %s",
            k2_readiness.get("resolved_agent_ref") or "no agent",
            BOOT.get("agent_ref") or "the configured agent",
        )
    slack_auth = slack_auth_readiness(os.getenv("SLACK_BOT_TOKEN", ""))
    # Stable Slack IDs are returned only by the authenticated identity endpoint.
    # /health publishes BOOT without authentication, so never retain them here.
    slack_auth.pop("identity", None)
    BOOT["slack_auth"] = slack_auth
    logger.info("config slack_auth: %s", slack_auth)
    if GATEWAY_ENABLED and slack_auth["auth_ok"] is False:
        logger.error("SLACK_BOT_TOKEN was rejected by Slack")
    elif GATEWAY_ENABLED and slack_auth["missing_core_scopes"]:
        logger.error(
            "Slack app is missing core scopes: %s",
            ", ".join(slack_auth["missing_core_scopes"]),
        )
    if GATEWAY_ENABLED and not BOOT.get("slack_admins_configured"):
        # Hermes disables slash-command gating entirely when no admin list is
        # configured, which means every workspace member can run /model, /yolo
        # and /cron. Loud, because it is silent otherwise.
        logger.warning(
            "SLACK_ADMIN_USERS is unset — slash-command gating is DISABLED and "
            "every workspace member can run every command"
        )
    if GATEWAY_ENABLED and not BOOT.get("slack_channel_allowlist"):
        logger.warning(
            "SLACK_ALLOWED_CHANNELS is unset — the agent will answer in any channel"
        )

    # The three old operator briefs produced status noise and stale autonomous
    # conclusions. Preserve their exact records on disk, then pause rather than
    # delete them. This runs before the gateway so the scheduler cannot claim a
    # due brief between inventory and retirement.
    if GATEWAY_ENABLED:
        channel = BOOT.get("home_channel_id") or ""
        if channel:
            # `SLACK_HOME_CHANNEL` is written operator-friendly as
            # "C0BN349TRU7|#cleo", and this service parses that. Hermes' cron
            # scheduler does NOT: `_get_home_target_chat_id` returns the raw env
            # value, so any job delivering to bare `slack` (or `all`) would post
            # to a chat id of "C0BN349TRU7|#cleo" and fail every week. Normalise
            # it here — the gateway child inherits this environment.
            os.environ["SLACK_HOME_CHANNEL"] = channel
        BOOT["cron_briefs"] = cron_seed.retire_stale_briefs()
        BOOT["cron_smoke"] = "retired-with-recurring-briefs"
        logger.info("config cron_briefs: %s", BOOT["cron_briefs"])

    if GATEWAY_ENABLED and not BOOT["gateway_start_allowed"]:
        supervisor.block_start(
            "gateway start blocked: Cleo has neither an applied active K2 runtime "
            "pack nor a declared K2 outage fallback with the mission-context plugin"
        )
        if BOOT.get("agent_ref") and k2_readiness.get("outage_declared") is not True:
            threading.Thread(
                target=_watch_for_k2_activation,
                name="k2-activation-watcher",
                daemon=True,
            ).start()
    else:
        supervisor.start()


def activation_readiness() -> dict[str, Any]:
    """Pre-activation body proof; intentionally excludes circular online state."""
    gateway = supervisor.snapshot()
    k2 = BOOT.get("k2_agent_readiness") or {}
    plugin = BOOT.get("k2_context_plugin") or {}
    slack_lead = BOOT.get("slack_agent_lead") or {}
    external_dispatch = BOOT.get("external_dispatch") or {}
    admission_ledger = BOOT.get("agent_run_ledger") or {}
    slack_auth = BOOT.get("slack_auth") or {}
    model_routes = BOOT.get("model_route_readiness") or []
    primary_route_ready = any(
        route.get("role") == "primary" and route.get("available") is True
        for route in model_routes
        if isinstance(route, Mapping)
    )
    checks = {
        "agent_ref_matches": BOOT.get("agent_ref") == "agent:cleo",
        "runtime_lane_matches": BOOT.get("runtime_lane") == "hermes",
        "config_written": BOOT.get("written") is True,
        "hook_token_configured": len(_hook_token()) >= 16,
        "hook_surface_configured": (
            external_dispatch.get("configured") is True
            and admission_ledger.get("ready") is True
        ),
        "runtime_cli_present": gateway.get("cli_present") is True,
        "channel_adapter_available": gateway.get("slack_adapter_available") is True,
        "mcp_sdk_available": gateway.get("mcp_sdk_available") is True,
        "channel_auth_ok": slack_auth.get("auth_ok") is True,
        "channel_scopes_ready": (
            slack_auth.get("scopes_known") is True
            and not bool(slack_auth.get("missing_core_scopes"))
        ),
        "primary_model_route_ready": primary_route_ready,
        "web_search_ready": (
            (BOOT.get("web_search_readiness") or {}).get("available") is True
        ),
        "k2_server_is_canonical": k2.get("server_matches_katailyst2") is True,
        "k2_runtime_pack_tool_listed": k2.get("runtime_pack_tool_listed") is True,
        "k2_well_tool_listed": k2.get("well_tool_listed") is True,
        "k2_runtime_pack_callable": k2.get("runtime_pack_callable") is True,
        "k2_agent_bound_token": k2.get("agent_bound_token") is True,
        "k2_identity_matches": k2.get("identity_matches") is True,
        "k2_host_profile_compatible": k2.get("host_profile_compatible") is True,
        "k2_context_plugin_ready": (
            plugin.get("installed") is True and plugin.get("enabled") is True
        ),
        "slack_agent_lead_ready": slack_lead.get("local_agent_ready") is True
        and (
            slack_lead.get("required") is not True
            or slack_lead.get("roster_ready") is True
        ),
    }
    return {
        "ready": all(checks.values()),
        "contractVersion": ACTIVATION_CONTRACT_VERSION,
        "stage": "pre_activation",
        "agentRef": BOOT.get("agent_ref") or "",
        "checks": checks,
    }


def external_dispatch_readiness() -> dict[str, Any]:
    gateway = supervisor.snapshot()
    k2 = BOOT.get("k2_agent_readiness") or {}
    plugin = BOOT.get("k2_context_plugin") or {}
    slack_lead = BOOT.get("slack_agent_lead") or {}
    slack_auth = BOOT.get("slack_auth") or {}
    model_routes = BOOT.get("model_route_readiness") or []
    primary_route_ready = any(
        route.get("role") == "primary" and route.get("available") is True
        for route in model_routes
        if isinstance(route, Mapping)
    )
    api = (
        hermes_api_readiness()
        if gateway.get("running")
        else {
            "reachable": False,
            "status": None,
            "error": "gateway is not running",
        }
    )
    checks = {
        "hook_token_configured": len(_hook_token()) >= 16,
        "admission_ledger_ready": (
            (BOOT.get("agent_run_ledger") or {}).get("ready") is True
        ),
        "gateway_running": gateway.get("running") is True,
        "slack_adapter_available": gateway.get("slack_adapter_available") is True,
        "slack_socket_connected": gateway.get("slack_socket_connected") is True,
        "slack_auth_ok": slack_auth.get("auth_ok") is True,
        "slack_scopes_ready": not bool(slack_auth.get("missing_core_scopes")),
        "primary_model_route_ready": primary_route_ready,
        "k2_runtime_pack_applied": BOOT.get("runtime_pack_applied") is True,
        "k2_agent_bound_token": k2.get("agent_bound_token") is True,
        "k2_runtime_pack_tool_callable": k2.get("runtime_pack_callable") is True,
        "k2_context_plugin_ready": (
            plugin.get("installed") is True and plugin.get("enabled") is True
        ),
        "slack_agent_lead_ready": slack_lead.get("local_agent_ready") is True
        and (
            slack_lead.get("required") is not True
            or slack_lead.get("roster_ready") is True
        ),
        "hermes_run_api_reachable": api.get("reachable") is True,
    }
    return {
        "ready": all(checks.values()),
        "agentRef": BOOT.get("agent_ref") or "",
        "checks": checks,
        "optionalChecks": {
            "k2_well_enrichment_callable": k2.get("well_callable") is True,
        },
        "hermesApi": api,
    }


@app.get("/activationz")
def activationz(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    if not _hook_authorized(authorization):
        return JSONResponse({"ready": False, "error": "unauthorized"}, status_code=401)
    readiness = activation_readiness()
    return JSONResponse(readiness, status_code=200 if readiness["ready"] else 503)


@app.get("/slack-identityz")
def slack_identityz(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    """Fresh, read-only Slack identity proof for K2's canonical binding writer."""
    if not _hook_authorized(authorization):
        return JSONResponse(
            {"ready": False, "error": "unauthorized"},
            status_code=401,
            headers=SLACK_IDENTITY_RESPONSE_HEADERS,
        )
    observed = slack_auth_readiness(os.getenv("SLACK_BOT_TOKEN", ""))
    checks = {
        "channel_auth_ok": observed.get("auth_ok") is True,
        "channel_scopes_ready": (
            observed.get("scopes_known") is True
            and not bool(observed.get("missing_core_scopes"))
        ),
        "identity_complete": observed.get("identity_ok") is True,
    }
    ready = all(checks.values())
    return JSONResponse(
        {
            "ready": ready,
            "contractVersion": SLACK_IDENTITY_CONTRACT_VERSION,
            "agentRef": BOOT.get("agent_ref") or "",
            "checks": checks,
            "identity": observed.get("identity"),
        },
        status_code=200 if ready else 503,
        headers=SLACK_IDENTITY_RESPONSE_HEADERS,
    )


@app.get("/readyz")
def readyz(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    if not _hook_authorized(authorization):
        return JSONResponse({"ready": False, "error": "unauthorized"}, status_code=401)
    readiness = external_dispatch_readiness()
    return JSONResponse(readiness, status_code=200 if readiness["ready"] else 503)


@app.post("/hooks/agent")
def agent_hook(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    if not _hook_token():
        return JSONResponse(
            {"ok": False, "error": "agent hook is not configured"}, status_code=503
        )
    if not _hook_authorized(authorization):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if BOOT.get("runtime_pack_applied") is not True:
        return JSONResponse(
            {"ok": False, "error": "canonical Cleo runtime pack is not active"},
            status_code=503,
        )
    try:
        response = dispatch_agent_hook(payload)
    except agent_run_ledger.AdmissionConflict as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("agent hook admission failed: %s", exc)
        return JSONResponse(
            {"ok": False, "error": "durable agent-run admission unavailable"},
            status_code=503,
        )
    return JSONResponse(response, status_code=202)


@app.get("/hooks/agent/runs/{run_id}")
def agent_hook_run(
    run_id: str,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    if not _hook_authorized(authorization):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        status, response = read_agent_hook_run(run_id)
    except Exception as exc:
        logger.error("agent hook status ledger failed: %s", exc)
        return JSONResponse(
            {"ok": False, "runId": run_id, "error": "run status unavailable"},
            status_code=503,
        )
    return JSONResponse(response, status_code=status)


@app.get("/health")
def health() -> dict[str, Any]:
    gateway = supervisor.snapshot()

    # A credential that cannot run inference is not a working agent: she hears
    # every message and answers each one "Provider authentication failed".
    # Only a positively-identified bad key degrades — "unknown" means the boot
    # check could not reach OpenRouter, which is not evidence of anything.
    active_provider = BOOT.get("model_provider") or ""
    if active_provider in {"xai-oauth", "openai-codex"}:
        active_subscription = BOOT.get("subscription_auth") or {}
        model_credentials_bad = (
            active_subscription.get("logged_in") is False
            or active_subscription.get("rate_limited") is True
        )
    elif active_provider == "openrouter":
        model_credentials_bad = not BOOT.get("openrouter_key_present") or BOOT.get(
            "openrouter_key_kind"
        ) in {"provisioning", "rejected"}
    else:
        model_credentials_bad = False
    # Mounted servers the agent cannot reach are worse than none: she reports
    # having them and then cannot answer from any of them.
    mcp_dead = bool(BOOT.get("mcp_mounted")) and not gateway["mcp_sdk_available"]
    slack_auth = BOOT.get("slack_auth") or {}
    slack_auth_bad = slack_auth.get("auth_ok") is False
    slack_scopes_bad = bool(slack_auth.get("missing_core_scopes"))
    model_routes = BOOT.get("model_route_readiness") or []
    fallback_routes_bad = any(
        route.get("role", "").startswith("fallback-")
        and route.get("available") is False
        for route in model_routes
    )
    k2_readiness = BOOT.get("k2_agent_readiness") or {}
    web_search_bad = (BOOT.get("web_search_readiness") or {}).get("available") is False
    k2_required = bool(BOOT.get("agent_ref"))
    k2_wrong_server = k2_required and (
        k2_readiness.get("server_matches_katailyst2") is False
    )
    k2_outage_fallback = k2_required and (
        BOOT.get("brain_source") == "bundled_outage_fallback"
    )
    k2_brain_bad = (
        k2_required
        and not k2_outage_fallback
        and (
            not k2_readiness.get("mounted")
            or not k2_readiness.get("bearer_present")
            or k2_readiness.get("transport_ok") is False
            or not k2_readiness.get("runtime_pack_tool_listed")
            or not k2_readiness.get("runtime_pack_callable")
            or not k2_readiness.get("agent_bound_token")
            or not k2_readiness.get("host_profile_compatible")
            or BOOT.get("runtime_pack_applied") is not True
        )
    )
    k2_context_bad = (
        k2_required
        and not k2_brain_bad
        and (
            not k2_readiness.get("well_tool_listed")
            or not k2_readiness.get("well_callable")
        )
    )
    plugin = BOOT.get("k2_context_plugin") or {}
    k2_plugin_bad = k2_required and (
        plugin.get("installed") is not True or plugin.get("enabled") is not True
    )
    slack_lead = BOOT.get("slack_agent_lead") or {}
    slack_lead_bad = k2_required and (
        slack_lead.get("local_agent_ready") is not True
        or (
            slack_lead.get("required") is True
            and slack_lead.get("roster_ready") is not True
        )
    )
    external_hook_bad = k2_required and (
        (BOOT.get("external_dispatch") or {}).get("configured") is not True
        or (BOOT.get("agent_run_ledger") or {}).get("ready") is not True
    )

    if not GATEWAY_ENABLED:
        status, mode = "ok", "readiness_gateway"
    elif gateway["running"] and gateway["slack_adapter_available"] and mcp_dead:
        status, mode = "degraded", "gateway_no_mcp_sdk"
    elif (
        gateway["running"]
        and gateway["slack_adapter_available"]
        and model_credentials_bad
    ):
        status, mode = "degraded", "gateway_no_model_credentials"
    elif gateway["running"] and gateway["slack_adapter_available"] and k2_wrong_server:
        status, mode = "degraded", "gateway_k2_wrong_server"
    elif (
        gateway["running"] and gateway["slack_adapter_available"] and k2_outage_fallback
    ):
        status, mode = "degraded", "gateway_k2_outage_fallback"
    elif gateway["running"] and gateway["slack_adapter_available"] and k2_brain_bad:
        status, mode = "degraded", "gateway_k2_brain_unavailable"
    elif gateway["running"] and gateway["slack_adapter_available"] and k2_plugin_bad:
        status, mode = "degraded", "gateway_k2_context_plugin_missing"
    elif (
        gateway["running"] and gateway["slack_adapter_available"] and external_hook_bad
    ):
        status, mode = "degraded", "gateway_external_dispatch_unavailable"
    elif gateway["running"] and gateway["slack_adapter_available"] and web_search_bad:
        status, mode = "degraded", "gateway_web_search_degraded"
    elif (
        gateway["running"]
        and gateway["slack_adapter_available"]
        and fallback_routes_bad
    ):
        status, mode = "degraded", "gateway_model_fallback_degraded"
    elif gateway["running"] and gateway["slack_adapter_available"] and slack_auth_bad:
        status, mode = "degraded", "gateway_slack_auth_failed"
    elif gateway["running"] and gateway["slack_adapter_available"] and slack_scopes_bad:
        status, mode = "degraded", "gateway_slack_scopes_missing"
    elif slack_lead_bad:
        status, mode = "degraded", "gateway_slack_agent_lead_unready"
    elif (
        gateway["running"]
        and gateway["slack_adapter_available"]
        # `.get`, not `[]`: a snapshot assembled anywhere else — an older cached
        # payload, a test double — must not turn /health into a 500. A missing
        # key reads as "not observed", which is the same as a fresh boot.
        and gateway.get("slack_socket_connected") is False
    ):
        # Deaf with every other light green. The adapter exists, the process is
        # up, the token is fine — and the websocket has been retrying into a
        # closed session. This is the branch that would have caught Cleo's
        # seven-day silence on day one; `is False` rather than falsy on purpose,
        # because None means "not observed yet" and must not degrade a boot.
        status, mode = "degraded", "gateway_slack_socket_down"
    elif gateway["running"] and gateway["slack_adapter_available"]:
        status, mode = "ok", "gateway"
    elif gateway["running"]:
        # Alive, but deaf: the process supervises cron happily while no Slack
        # adapter exists. Reporting this as healthy is how a bot sits silent
        # for days behind a green check.
        status, mode = "degraded", "gateway_no_slack_adapter"
    else:
        status, mode = "degraded", "gateway_down"

    payload: dict[str, Any] = {
        "status": status,
        "service": "hlt-agent",
        "agent": BOOT.get("agent"),
        "mode": mode,
        "hermes_home": str(HERMES_HOME),
        "config": BOOT,
        "gateway": gateway,
    }
    if k2_context_bad:
        payload["advisories"] = [
            {
                "code": "k2_well_enrichment_unavailable",
                "impact": (
                    "Optional automatic task-specific enrichment was unavailable "
                    "at boot. The canonical runtime pack and direct K2 reads remain "
                    "the working context path."
                ),
            }
        ]
    if mode == "readiness_gateway":
        payload["note"] = (
            "The agent is installed and configured but the Slack gateway is off. "
            "Set SLACK_BOT_TOKEN, SLACK_APP_TOKEN and AGENT_ENABLE_GATEWAY=1 to "
            "bring it up — see services/agent/README.md."
        )
    elif mode == "gateway_no_mcp_sdk":
        payload["note"] = (
            f"{len(BOOT.get('mcp_mounted') or [])} MCP servers are configured and "
            "granted, but the `mcp` python package is not installed, so Hermes "
            "disabled MCP entirely and the agent has none of their tools. It is "
            "an optional upstream extra — the image must install "
            "hermes-agent[mcp]."
        )
    elif mode == "gateway_no_model_credentials":
        if active_provider in {"xai-oauth", "openai-codex"}:
            if (BOOT.get("subscription_auth") or {}).get("rate_limited") is True:
                reset_at = (BOOT.get("subscription_auth") or {}).get("reset_at")
                reset_note = f" Reset is reported at {reset_at}." if reset_at else ""
                payload["note"] = (
                    f"Slack is connected and the active {active_provider} OAuth "
                    "profile is valid, but its subscription quota is exhausted."
                    + reset_note
                )
            else:
                payload["note"] = (
                    f"Slack is connected, but the active {active_provider} subscription "
                    "OAuth credential is not logged in. Re-run the provider device-code "
                    "login before treating Cleo as able to answer."
                )
        else:
            payload["note"] = (
                "Slack is connected, but OPENROUTER_API_KEY cannot run inference "
                f"(kind: {BOOT.get('openrouter_key_kind')}). The bot receives every "
                "message and replies 'Provider authentication failed'. A provisioning "
                "key looks identical to an inference key — check /api/v1/key."
            )
    elif mode == "gateway_no_slack_adapter":
        payload["note"] = (
            "The gateway is running but Hermes could not build a Slack adapter, "
            "so the bot is connected to nothing. slack-bolt is an optional "
            "upstream extra — the image must install hermes-agent[slack]."
        )
    elif mode == "gateway_k2_outage_fallback":
        payload["note"] = (
            "Katailyst2 declared a transport/service outage at boot, so Cleo is "
            "running from the reviewed bundled SOUL/AGENTS snapshot. This is an "
            "explicit degraded fallback, never a substitute for a missing or "
            "mis-scoped agent token."
        )
    elif mode == "gateway_k2_brain_unavailable":
        payload["note"] = (
            "Cleo did not boot the active agent:cleo runtime pack with an "
            "agent-bound K2 token and a compatible paperclip_hermes host profile. "
            "A mount alone is not a brain; read config.k2_agent_readiness."
        )
    elif mode == "gateway_k2_wrong_server":
        payload["note"] = (
            "The configured MCP endpoint answered, but it did not identify itself "
            "as Katailyst2. Cleo must use the canonical v2 door, not the legacy v1 "
            "bridge. Read config.k2_agent_readiness.server_repo."
        )
    elif mode == "gateway_k2_context_plugin_missing":
        payload["note"] = (
            "The canonical K2 pack is present, but Hermes did not install and "
            "enable the hlt-k2-context pre_llm_call hook. A model turn would not "
            "receive its one bounded task-specific K2 draw."
        )
    elif mode == "gateway_slack_agent_lead_unready":
        payload["note"] = (
            "The private Slack lead selector is not ready, so shared messages "
            "fail closed before typing or model dispatch. Read "
            "config.slack_agent_lead for the roster or local-identity mismatch."
        )
    elif mode == "gateway_external_dispatch_unavailable":
        payload["note"] = (
            "Slack work is available, but the authenticated K2 external-run "
            "bridge is not configured. Set the shared agent-hook credential so "
            "K2 can dispatch and poll Cleo's native Hermes runs."
        )
    elif mode == "gateway_model_fallback_degraded":
        unavailable = [
            f"{route.get('provider')}/{route.get('model')}"
            for route in model_routes
            if route.get("role", "").startswith("fallback-")
            and route.get("available") is False
        ]
        payload["note"] = (
            "The primary model route is ready, but one or more configured recovery "
            "routes have a positively missing/rejected credential: "
            + ", ".join(unavailable)
        )
    elif mode == "gateway_web_search_degraded":
        search = BOOT.get("web_search_readiness") or {}
        payload["note"] = (
            f"Hermes selected the {search.get('provider') or 'configured'} web "
            "search backend, but its credential or installed SDK is missing. "
            "Read config.web_search_readiness before calling web research ready."
        )
    elif mode == "gateway_slack_socket_down":
        failure = gateway.get("slack_socket_last_failure") or "no marker captured"
        ago = gateway.get("slack_socket_connected_seconds_ago")
        last_good = (
            f"last connected {round(ago / 3600, 1)}h ago"
            if ago
            else "never connected since this process started"
        )
        payload["note"] = (
            "The gateway process is up and Slack authentication is fine, but the "
            "Socket Mode websocket is not connected, so Cleo cannot hear anyone — "
            f"{gateway.get('slack_socket_failures_since_connect')} consecutive connect failures, "
            f"{last_good}. Last failure: {failure}. Restarting the service reconnects it; "
            "if it recurs, the adapter is reusing a closed aiohttp session on reconnect."
        )
    elif mode == "gateway_slack_auth_failed":
        payload["note"] = (
            "The gateway is running, but Slack rejected SLACK_BOT_TOKEN. Reinstall "
            "the app or update the bot token before treating Cleo as reachable."
        )
    elif mode == "gateway_slack_scopes_missing":
        payload["note"] = (
            "Slack authentication works, but Cleo is missing core scopes needed "
            "for channel/DM work or file delivery: "
            + ", ".join(slack_auth.get("missing_core_scopes") or [])
        )
    elif mode == "gateway_down":
        payload["note"] = gateway["stopped_reason"] or (
            "Gateway was requested but is not running; check the service logs."
        )
    return payload


def main() -> None:
    signal.signal(signal.SIGTERM, supervisor.shutdown)
    signal.signal(signal.SIGINT, supervisor.shutdown)
    boot()
    try:
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
    finally:
        supervisor.shutdown()


if __name__ == "__main__":
    main()
