"""Port-owner and supervisor for the ``hlt-hermes`` Render service.

Render runs this as a **web service**: something has to bind ``$PORT`` or the
health check fails and the deploy is rolled back. Hermes' own Slack gateway
talks to Slack over Socket Mode — an outbound WebSocket — and never binds a
port, so it cannot be the container's main process on its own.

So this module owns the port, boots Hermes' config, supervises the gateway as
a child process, and reports what actually happened. ``/health`` answers from
observed state (is the child alive? did the config get written?) rather than
from "an env var is set", so a green check means the agent is really up.
"""
from __future__ import annotations

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

import render_config
import seed_memory

logging.basicConfig(level=logging.INFO, format="[hermes] %(levelname)s %(message)s")
logger = logging.getLogger("hlt-hermes")

HERMES_HOME = Path(os.getenv("HERMES_HOME", "/data/hermes"))
GATEWAY_ENABLED = os.getenv("HERMES_ENABLE_GATEWAY", "0") == "1"

# Give up after this many crashes so a bad config surfaces in /health instead
# of hiding behind an endless restart loop.
MAX_RESTARTS = 5
BACKOFF_CAP_SECONDS = 60

app = FastAPI(title="HLT Hermes")


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

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            return {
                "requested": GATEWAY_ENABLED,
                "running": running,
                "cli_present": self.cli_present,
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
            logger.info("gateway disabled (HERMES_ENABLE_GATEWAY != 1) — serving health only")
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
                self._proc = subprocess.Popen(["hermes", "gateway"], cwd=str(HERMES_HOME))
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
    seed_memory.main()
    BOOT.update(render_config.render())
    for key, value in BOOT.items():
        logger.info("config %s: %s", key, value)
    if not BOOT.get("openrouter_key_present"):
        logger.warning("OPENROUTER_API_KEY is not set — Hermes has no model credentials")
    supervisor.start()


@app.get("/health")
def health() -> dict[str, Any]:
    gateway = supervisor.snapshot()
    memory_dir = HERMES_HOME / "memory"
    seeds = sorted(p.name for p in memory_dir.glob("*.md")) if memory_dir.exists() else []

    if not GATEWAY_ENABLED:
        status, mode = "ok", "readiness_gateway"
    elif gateway["running"]:
        status, mode = "ok", "gateway"
    else:
        status, mode = "degraded", "gateway_down"

    payload: dict[str, Any] = {
        "status": status,
        "service": "hlt-hermes",
        "mode": mode,
        "hermes_home": str(HERMES_HOME),
        "seeded_memory": seeds,
        "config": BOOT,
        "gateway": gateway,
    }
    if mode == "readiness_gateway":
        payload["note"] = (
            "Hermes is installed and configured but the Slack gateway is off. "
            "Set SLACK_BOT_TOKEN, SLACK_APP_TOKEN and HERMES_ENABLE_GATEWAY=1 to "
            "bring the agent up — see services/hermes/README.md."
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
