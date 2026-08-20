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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import cron_seed
import grounding
import render_config
import uvicorn
from fastapi import FastAPI

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
                except Exception:  # noqa: BLE001 - a watcher must never kill the log stream
                    logger.exception("socket-state watcher failed on a log line")
        except Exception:  # noqa: BLE001 - the child dying mid-read is the supervisor's business
            logger.exception("gateway log pump stopped")

    def start(self) -> None:
        if not GATEWAY_ENABLED:
            logger.info("gateway disabled (AGENT_ENABLE_GATEWAY != 1) — serving health only")
            return
        if not self.cli_present:
            self._gave_up_reason = (
                "hermes CLI not found on PATH; the image failed to install hermes-agent"
            )
            logger.error(self._gave_up_reason)
            return
        threading.Thread(target=self._supervise, name="hermes-gateway", daemon=True).start()

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
                target=self._pump_output, args=(proc,), name="hermes-gateway-logs", daemon=True
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
MCP_PROTOCOL_VERSION = "2025-06-18"

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
        response_headers = {key.lower(): value for key, value in response.headers.items()}
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


def _well_agent_ref(data: Mapping[str, Any], expected_agent_ref: str) -> str:
    """Return the exact agent block from the current wishing-well shape.

    ``katailyst.well`` returns ``dives[].blocks[]``. Agent blocks carry the
    canonical ``typedRef`` (for example ``agent:cleo``); the separate ``type``
    and ``ref`` fields are accepted as a conservative fallback for older K2
    renderers. Never infer an identity from names or approximate search scores.
    """
    expected_bare = expected_agent_ref.split("@", 1)[0]
    dives = data.get("dives")
    if not isinstance(dives, list):
        return ""
    for dive in dives:
        if not isinstance(dive, Mapping):
            continue
        blocks = dive.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            typed_ref = str(block.get("typedRef") or "")
            if not typed_ref:
                block_type = str(block.get("type") or "")
                block_ref = str(block.get("ref") or "")
                if block_type and block_ref:
                    typed_ref = (
                        block_ref
                        if block_ref.startswith(f"{block_type}:")
                        else f"{block_type}:{block_ref}"
                    )
            if typed_ref.split("@", 1)[0] == expected_bare:
                return typed_ref
    return ""


def k2_agent_readiness(
    url: str,
    token: str,
    expected_agent_ref: str,
    runtime_lane: str = "hermes",
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Prove the current K2 team-lane door can discover Cleo's agent block.

    A configured URL, token, MCP SDK and even a healthy ``tools/list`` do not
    prove ``agent:cleo`` exists. This uses the same bearer and endpoint Hermes
    receives, then calls the listed ``katailyst.well`` tool with its current
    schema and an exact agent facet. Runtime lane is host metadata only; it is
    deliberately not sent as a made-up K2 argument.
    """
    started = time.monotonic()
    result: dict[str, Any] = {
        "mounted": bool(url),
        "bearer_present": bool(token),
        "transport_ok": None,
        "server_repo": "",
        "server_matches_katailyst2": None,
        "visible_tools": None,
        "well_tool_listed": False,
        "well_callable": False,
        "agent_block_found": False,
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
        request_id += 1
        return _mcp_post(
            url,
            token,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            session_id=session_id,
            timeout=timeout,
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
        if initialized.get("error"):
            raise RuntimeError(str(initialized["error"]))
        result["transport_ok"] = True
        result["server_repo"] = headers.get("x-katailyst-repo", "")
        result["server_matches_katailyst2"] = (
            result["server_repo"].strip().lower() == "katailyst2"
        )
        if result["server_matches_katailyst2"] is not True:
            result["contract_status"] = "wrong_server"
            return result

        listed, session_id, _ = rpc("tools/list", {}, session_id)
        if listed.get("error"):
            raise RuntimeError(str(listed["error"]))
        tools = (listed.get("result") or {}).get("tools") or []
        names = [
            str(tool.get("name") or "")
            for tool in tools
            if isinstance(tool, dict)
        ]
        result["visible_tools"] = len(names)
        tool_name = next(
            (name for name in names if name in {"katailyst.well", "katailyst_well"}),
            "",
        )
        result["well_tool_listed"] = bool(tool_name)
        if not tool_name:
            result["contract_status"] = "tool_not_listed"
            return result
        if not expected_agent_ref:
            result["contract_status"] = "not_requested"
            return result

        called, _, _ = rpc(
            "tools/call",
            {
                "name": tool_name,
                "arguments": {
                    "mission": (
                        f"Load the current runtime pack and useful capabilities for "
                        f"{expected_agent_ref}."
                    ),
                    "facets": [f"{expected_agent_ref} runtime pack"],
                    "budget": 12,
                    "thoughts": False,
                    "traverse": False,
                },
            },
            session_id,
        )
        tool_result = called.get("result") or {}
        if called.get("error") or tool_result.get("isError"):
            raise RuntimeError(str(called.get("error") or "katailyst.well returned isError"))
        result["well_callable"] = True
        data = _mcp_tool_data(tool_result)
        resolved_ref = _well_agent_ref(data, expected_agent_ref)
        requested_bare = expected_agent_ref.split("@", 1)[0]
        resolved_bare = resolved_ref.split("@", 1)[0]
        result["agent_block_found"] = bool(resolved_ref)
        result["contract_status"] = "loaded" if resolved_ref else "not_found"
        result["resolved_agent_ref"] = resolved_ref
        result["identity_matches"] = bool(resolved_ref) and requested_bare == resolved_bare
        return result
    except Exception as exc:
        if result["transport_ok"] is None:
            result["transport_ok"] = False
        result["contract_status"] = "unavailable"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        return result
    finally:
        result["latency_ms"] = round((time.monotonic() - started) * 1000)


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


def web_search_readiness(
    backend: str, env: Mapping[str, str]
) -> dict[str, Any]:
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
    if not result["auth_ok"] or not raw_scopes:
        return result

    scopes = sorted({scope.strip() for scope in raw_scopes.split(",") if scope.strip()})
    result["scopes_known"] = True
    result["granted_scopes"] = scopes
    result["missing_core_scopes"] = sorted(CORE_SLACK_SCOPES - set(scopes))
    result["artifact_delivery_ready"] = "files:write" in scopes
    return result


def boot() -> None:
    BOOT.clear()
    BOOT.update(grounding.install())
    BOOT.update(render_config.render())
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
        logger.warning("OPENROUTER_API_KEY is not set — the active provider has no credentials")
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

    k2_readiness = k2_agent_readiness(
        os.getenv("KATAILYST2_MCP_URL", "").strip(),
        os.getenv("KATAILYST2_MCP_TOKEN", "").strip(),
        str(BOOT.get("agent_ref") or ""),
        str(BOOT.get("runtime_lane") or "hermes"),
    )
    BOOT["k2_agent_readiness"] = k2_readiness
    logger.info("config k2_agent_readiness: %s", k2_readiness)
    if BOOT.get("agent_ref") and k2_readiness["contract_status"] != "loaded":
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
        logger.warning("SLACK_ALLOWED_CHANNELS is unset — the agent will answer in any channel")

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

    supervisor.start()


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
        model_credentials_bad = (
            not BOOT.get("openrouter_key_present")
            or BOOT.get("openrouter_key_kind") in {"provisioning", "rejected"}
        )
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
    web_search_bad = (
        (BOOT.get("web_search_readiness") or {}).get("available") is False
    )
    k2_required = bool(BOOT.get("agent_ref"))
    k2_wrong_server = k2_required and (
        k2_readiness.get("server_matches_katailyst2") is False
    )
    k2_transport_bad = k2_required and (
        not k2_readiness.get("mounted")
        or not k2_readiness.get("bearer_present")
        or k2_readiness.get("transport_ok") is False
        or not k2_readiness.get("well_tool_listed")
        or not k2_readiness.get("well_callable")
    )
    k2_contract_bad = k2_required and (
        k2_readiness.get("contract_status") != "loaded"
        or k2_readiness.get("identity_matches") is not True
    )

    if not GATEWAY_ENABLED:
        status, mode = "ok", "readiness_gateway"
    elif gateway["running"] and gateway["slack_adapter_available"] and mcp_dead:
        status, mode = "degraded", "gateway_no_mcp_sdk"
    elif gateway["running"] and gateway["slack_adapter_available"] and model_credentials_bad:
        status, mode = "degraded", "gateway_no_model_credentials"
    elif gateway["running"] and gateway["slack_adapter_available"] and k2_wrong_server:
        status, mode = "degraded", "gateway_k2_wrong_server"
    elif gateway["running"] and gateway["slack_adapter_available"] and k2_transport_bad:
        status, mode = "degraded", "gateway_k2_unreachable"
    elif gateway["running"] and gateway["slack_adapter_available"] and k2_contract_bad:
        status, mode = "degraded", "gateway_k2_contract_missing"
    elif gateway["running"] and gateway["slack_adapter_available"] and web_search_bad:
        status, mode = "degraded", "gateway_web_search_degraded"
    elif gateway["running"] and gateway["slack_adapter_available"] and fallback_routes_bad:
        status, mode = "degraded", "gateway_model_fallback_degraded"
    elif gateway["running"] and gateway["slack_adapter_available"] and slack_auth_bad:
        status, mode = "degraded", "gateway_slack_auth_failed"
    elif gateway["running"] and gateway["slack_adapter_available"] and slack_scopes_bad:
        status, mode = "degraded", "gateway_slack_scopes_missing"
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
    elif mode == "gateway_k2_unreachable":
        payload["note"] = (
            "Cleo's Katailyst2 route is not fully callable with the same endpoint "
            "and bearer Hermes receives. Mounted is configuration, not a working "
            "capability. Read config.k2_agent_readiness for the missing seam or "
            "bounded transport error."
        )
    elif mode == "gateway_k2_wrong_server":
        payload["note"] = (
            "The configured MCP endpoint answered, but it did not identify itself "
            "as Katailyst2. Cleo must use the canonical v2 door, not the legacy v1 "
            "bridge. Read config.k2_agent_readiness.server_repo."
        )
    elif mode == "gateway_k2_contract_missing":
        payload["note"] = (
            f"Katailyst2's wishing well is callable, but the exact agent facet did "
            f"not return {BOOT.get('agent_ref') or 'agent:cleo'}. Cleo can still "
            "search K2, but this deploy has not proved her canonical agent block. "
            "Read config.k2_agent_readiness.contract_status and resolved_agent_ref."
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
