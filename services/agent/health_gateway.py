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

import grounding
import render_config

logging.basicConfig(level=logging.INFO, format="[brian] %(levelname)s %(message)s")
logger = logging.getLogger("brian")

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
    """The argv for the gateway child, including its verbosity flag."""
    cmd = ["hermes", "gateway"]
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


def boot() -> None:
    BOOT.update(grounding.install())
    BOOT.update(render_config.render())
    for key, value in BOOT.items():
        logger.info("config %s: %s", key, value)
    if not BOOT.get("openrouter_key_present"):
        logger.warning("OPENROUTER_API_KEY is not set — the agent has no model credentials")
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
    supervisor.start()


@app.get("/health")
def health() -> dict[str, Any]:
    gateway = supervisor.snapshot()

    if not GATEWAY_ENABLED:
        status, mode = "ok", "readiness_gateway"
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
    elif mode == "gateway_no_slack_adapter":
        payload["note"] = (
            "The gateway is running but Hermes could not build a Slack adapter, "
            "so the bot is connected to nothing. slack-bolt is an optional "
            "upstream extra — the image must install hermes-agent[slack]."
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
