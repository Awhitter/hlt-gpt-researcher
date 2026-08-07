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
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

import cron_seed
import grounding
import render_config

logging.basicConfig(level=logging.INFO, format="[hlt-agent] %(levelname)s %(message)s")
logger = logging.getLogger("hlt-agent")

HERMES_HOME = Path(os.getenv("HERMES_HOME", "/data/hermes"))
GATEWAY_ENABLED = os.getenv("AGENT_ENABLE_GATEWAY", "0") == "1"

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
        self._proc: subprocess.Popen[bytes] | None = None
        self._stop = threading.Event()
        self._restarts = 0
        self._last_exit_code: int | None = None
        self._gave_up_reason: str | None = None
        self._started_at: float | None = None

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
            return {
                "requested": GATEWAY_ENABLED,
                "running": running,
                "cli_present": self.cli_present,
                "slack_adapter_available": self.slack_adapter_available,
                "mcp_sdk_available": self.mcp_sdk_available,
                "restarts": self._restarts,
                "last_exit_code": self._last_exit_code,
                "uptime_seconds": (
                    round(time.time() - self._started_at, 1)
                    if running and self._started_at
                    else None
                ),
                "stopped_reason": self._gave_up_reason,
            }

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
                self._proc = subprocess.Popen(cmd, cwd=str(HERMES_HOME))
                self._started_at = time.time()
                proc = self._proc

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
    BOOT.update(grounding.install())
    BOOT.update(render_config.render())
    for key, value in BOOT.items():
        logger.info("config %s: %s", key, value)
    if not BOOT.get("openrouter_key_present"):
        logger.warning("OPENROUTER_API_KEY is not set — the agent has no model credentials")
    else:
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

    # Seed the recurring briefs before the gateway starts, so the scheduler
    # reads a complete record on its first pass rather than one boot later.
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
            BOOT["cron_briefs"] = cron_seed.seed(f"slack:{channel}")
            BOOT["cron_smoke"] = cron_seed.seed_smoke(f"slack:{channel}")
            logger.info(
                "config cron_briefs: %s (smoke: %s)",
                BOOT["cron_briefs"], BOOT["cron_smoke"],
            )
        else:
            # Without a home channel a brief has nowhere to land, and a job
            # created with a bad deliver target fails silently every week.
            BOOT["cron_briefs"] = {"created": [], "existing": [], "failed": []}
            logger.warning(
                "SLACK_HOME_CHANNEL is unset — the recurring briefs were NOT "
                "created, because there is no channel to deliver them to"
            )

    supervisor.start()


@app.get("/health")
def health() -> dict[str, Any]:
    gateway = supervisor.snapshot()

    # A credential that cannot run inference is not a working agent: she hears
    # every message and answers each one "Provider authentication failed".
    # Only a positively-identified bad key degrades — "unknown" means the boot
    # check could not reach OpenRouter, which is not evidence of anything.
    model_credentials_bad = BOOT.get("openrouter_key_kind") in {"provisioning", "rejected"}
    # Mounted servers the agent cannot reach are worse than none: she reports
    # having them and then cannot answer from any of them.
    mcp_dead = bool(BOOT.get("mcp_mounted")) and not gateway["mcp_sdk_available"]
    slack_auth = BOOT.get("slack_auth") or {}
    slack_auth_bad = slack_auth.get("auth_ok") is False
    slack_scopes_bad = bool(slack_auth.get("missing_core_scopes"))

    if not GATEWAY_ENABLED:
        status, mode = "ok", "readiness_gateway"
    elif gateway["running"] and gateway["slack_adapter_available"] and mcp_dead:
        status, mode = "degraded", "gateway_no_mcp_sdk"
    elif gateway["running"] and gateway["slack_adapter_available"] and model_credentials_bad:
        status, mode = "degraded", "gateway_no_model_credentials"
    elif gateway["running"] and gateway["slack_adapter_available"] and slack_auth_bad:
        status, mode = "degraded", "gateway_slack_auth_failed"
    elif gateway["running"] and gateway["slack_adapter_available"] and slack_scopes_bad:
        status, mode = "degraded", "gateway_slack_scopes_missing"
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
