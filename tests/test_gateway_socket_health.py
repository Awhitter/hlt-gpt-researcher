"""The health check must be able to see a dead Slack socket.

Cleo answered nobody in Slack from 2026-08-09T03:14:58Z until 2026-08-16, and
``/health`` returned ``{"status": "ok", "mode": "gateway"}`` for all seven days.
Nothing was broken in a way anything looked at: ``gateway.running`` asks whether
the child PROCESS is alive, and ``slack_adapter_available`` is an
``importlib.util.find_spec`` call made once at boot. Neither can observe a
websocket. Render never restarted her because the check kept passing.

The module's own docstring warned about this shape — *"Alive, but deaf: …
Reporting this as healthy is how a bot sits silent for days behind a green
check"* — but the branch it guarded covers a MISSING adapter, not a
DISCONNECTED transport, so the real failure walked straight past it.

These tests pin the observation, not the wording: feed the supervisor the exact
log lines the adapter emitted during the outage and assert the state it reports.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1] / "services" / "agent"


def _load_health_gateway():
    """Import the module without booting the service (it binds a port at __main__)."""
    sys.path.insert(0, str(AGENT_DIR))
    spec = importlib.util.spec_from_file_location(
        "health_gateway_under_test", AGENT_DIR / "health_gateway.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import wiring
        pytest.skip("health_gateway.py is not importable in this environment")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - missing optional deps in CI
        pytest.skip(f"health_gateway.py dependencies unavailable: {exc}")
    return module


health_gateway = _load_health_gateway()


# The literal lines Render captured during the outage.
CONNECT_LINE = "INFO slack_bolt.AsyncApp: ⚡️ Bolt app is running!"
RETRY_LINE = (
    "ERROR slack_bolt.AsyncApp: Failed to connect (error: Session is closed); Retrying..."
)
UNHEALTHY_LINE = (
    "WARNING hermes_plugins.slack_platform.adapter: [Slack] Socket Mode unhealthy "
    "(transport disconnected); reconnecting"
)


@pytest.fixture()
def supervisor():
    return health_gateway.GatewaySupervisor()


def test_unobserved_socket_is_unknown_not_broken(supervisor):
    """A boot that has logged nothing yet must not degrade — None, never False."""
    assert supervisor._socket_state() is None


def test_a_successful_connect_reads_as_connected(supervisor):
    supervisor._note_gateway_line(CONNECT_LINE)
    assert supervisor._socket_state() is True


def test_the_real_outage_reads_as_disconnected(supervisor):
    """Connect, then the retry storm that actually happened."""
    supervisor._note_gateway_line(CONNECT_LINE)
    supervisor._note_gateway_line(UNHEALTHY_LINE)
    for _ in range(health_gateway.SOCKET_FAILURE_TOLERANCE):
        supervisor._note_gateway_line(RETRY_LINE)
    assert supervisor._socket_state() is False
    snapshot = supervisor.snapshot()
    assert snapshot["slack_socket_connected"] is False
    assert "Session is closed" in (snapshot["slack_socket_last_failure"] or "")


def test_a_single_dropped_frame_is_not_an_outage(supervisor):
    """Slack rotates sockets; one reconnect is normal and must not page anyone."""
    supervisor._note_gateway_line(CONNECT_LINE)
    supervisor._note_gateway_line(RETRY_LINE)
    assert supervisor._socket_state() is True


def test_recovery_clears_the_failure_streak(supervisor):
    supervisor._note_gateway_line(CONNECT_LINE)
    for _ in range(health_gateway.SOCKET_FAILURE_TOLERANCE + 3):
        supervisor._note_gateway_line(RETRY_LINE)
    assert supervisor._socket_state() is False
    supervisor._note_gateway_line(CONNECT_LINE)
    assert supervisor._socket_state() is True
    assert supervisor.snapshot()["slack_socket_failures_since_connect"] == 0


def test_the_log_pump_forwards_every_line(supervisor, capsys):
    """Piping the child is only acceptable if Render's stream is unchanged by it."""

    class FakeProc:
        stdout = iter([f"{CONNECT_LINE}\n", "some unrelated line\n", f"{RETRY_LINE}\n"])

    supervisor._pump_output(FakeProc())  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert CONNECT_LINE in out
    assert "some unrelated line" in out
    assert RETRY_LINE in out


def test_a_watcher_crash_never_swallows_a_log_line(supervisor, capsys, monkeypatch):
    monkeypatch.setattr(
        supervisor, "_note_gateway_line", lambda line: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    class FakeProc:
        stdout = iter(["first\n", "second\n"])

    supervisor._pump_output(FakeProc())  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "first" in out and "second" in out


def test_health_reports_degraded_for_the_exact_outage(monkeypatch):
    """The end-to-end assertion: the /health ladder, not just the snapshot field.

    This is the one that matters. Every other check in this file could pass while
    `/health` still answered `ok`, because the bug was never in observing the
    socket — nothing observed it — it was that the status ladder had no branch
    that could express "adapter present, transport dead".
    """
    monkeypatch.setattr(health_gateway, "GATEWAY_ENABLED", True)
    monkeypatch.setattr(
        health_gateway,
        "BOOT",
        {"agent": "cleo", "model_provider": "xai-oauth", "subscription_auth": {"logged_in": True}},
    )

    supervisor = health_gateway.GatewaySupervisor()
    supervisor._note_gateway_line(CONNECT_LINE)
    supervisor._note_gateway_line(UNHEALTHY_LINE)
    for _ in range(health_gateway.SOCKET_FAILURE_TOLERANCE):
        supervisor._note_gateway_line(RETRY_LINE)

    # The process is alive and the adapter imports fine — exactly Cleo's state.
    monkeypatch.setattr(
        type(supervisor), "slack_adapter_available", property(lambda self: True)
    )
    monkeypatch.setattr(type(supervisor), "mcp_sdk_available", property(lambda self: True))
    monkeypatch.setattr(type(supervisor), "cli_present", property(lambda self: True))
    real_snapshot = supervisor.snapshot()
    real_snapshot["running"] = True
    monkeypatch.setattr(health_gateway, "supervisor", supervisor)
    monkeypatch.setattr(supervisor, "snapshot", lambda: real_snapshot)

    payload = health_gateway.health()
    assert payload["status"] == "degraded", "a deaf bot must not report ok"
    assert payload["mode"] == "gateway_slack_socket_down"
    assert "cannot hear anyone" in payload["note"]


def test_health_still_reports_ok_when_the_socket_is_up(monkeypatch):
    """The other half of the control: the new branch must not degrade a healthy bot."""
    monkeypatch.setattr(health_gateway, "GATEWAY_ENABLED", True)
    monkeypatch.setattr(
        health_gateway,
        "BOOT",
        {"agent": "cleo", "model_provider": "xai-oauth", "subscription_auth": {"logged_in": True}},
    )
    supervisor = health_gateway.GatewaySupervisor()
    supervisor._note_gateway_line(CONNECT_LINE)
    monkeypatch.setattr(
        type(supervisor), "slack_adapter_available", property(lambda self: True)
    )
    monkeypatch.setattr(type(supervisor), "mcp_sdk_available", property(lambda self: True))
    monkeypatch.setattr(type(supervisor), "cli_present", property(lambda self: True))
    snap = supervisor.snapshot()
    snap["running"] = True
    monkeypatch.setattr(health_gateway, "supervisor", supervisor)
    monkeypatch.setattr(supervisor, "snapshot", lambda: snap)

    payload = health_gateway.health()
    assert payload["status"] == "ok"
    assert payload["mode"] == "gateway"


def test_cleo_runs_the_reviewed_model_route():
    """Pin the reviewed route: Codex gpt-5.6-sol primary, Grok 4.6 fallback.

    This test previously pinned grok-4.6 as DEFAULT_MODEL and a
    PROVIDER_DEFAULT_MODELS mapping that no longer exists; render_config moved
    to openai-codex/gpt-5.6-sol primary with xai-oauth/grok-4.6 as the one
    independent fallback route, and the test was left red. Pin what actually
    serves, so the next route change has to come here and say so.
    """
    sys.path.insert(0, str(AGENT_DIR))
    import render_config  # noqa: PLC0415 - imported lazily so the skip above can fire first

    assert render_config.DEFAULT_PROVIDER == "openai-codex"
    assert render_config.DEFAULT_MODEL == "gpt-5.6-sol"
    assert {"provider": "xai-oauth", "model": "grok-4.6"} in (
        render_config.DEFAULT_FALLBACK_PROVIDERS
    )


# ── Watchdog: the 2026-09 regression ─────────────────────────────────────────
# Observing the outage turned out not to be enough. 2026-09-08→11 the gateway
# logged "RuntimeError: Session is closed" 77,472 times over 72 hours while
# /health said `slack_socket_connected: false` the whole way — and `restarts`
# stayed 0, because `_supervise` only acts when the child EXITS and nothing
# acted on the observation. These tests pin the acting layer: the watchdog's
# bounce decision, its outage clock, and the streak cap that keeps it from
# thrashing a transport a restart cannot fix.


class LiveProc:
    """A child that is alive; the watchdog asks ``poll()``, snapshots ask ``pid``."""

    pid = 4242

    def poll(self):
        return None


class ExitedProc:
    def poll(self):
        return 1


def _drive_into_outage(supervisor) -> None:
    """Connect once, then fail past tolerance — the observed state flips False."""
    supervisor._note_gateway_line(CONNECT_LINE)
    for _ in range(health_gateway.SOCKET_FAILURE_TOLERANCE):
        supervisor._note_gateway_line(RETRY_LINE)


def _after_threshold() -> float:
    return time.time() + health_gateway.SOCKET_DEAD_RESTART_SECONDS + 1


def test_the_outage_clock_starts_when_tolerance_is_crossed(supervisor):
    supervisor._note_gateway_line(CONNECT_LINE)
    for _ in range(health_gateway.SOCKET_FAILURE_TOLERANCE - 1):
        supervisor._note_gateway_line(RETRY_LINE)
    assert supervisor._socket_down_since is None, "still within tolerance"
    supervisor._note_gateway_line(RETRY_LINE)
    assert supervisor._socket_down_since is not None
    watchdog = supervisor.snapshot()["slack_socket_watchdog"]
    assert watchdog["enabled"] is True
    assert watchdog["down_for_seconds"] is not None
    assert watchdog["exhausted"] is False


def test_no_bounce_while_the_socket_is_healthy(supervisor):
    supervisor._proc = LiveProc()
    supervisor._note_gateway_line(CONNECT_LINE)
    assert supervisor._watchdog_should_bounce(_after_threshold()) is False


def test_no_bounce_before_the_threshold(supervisor):
    supervisor._proc = LiveProc()
    _drive_into_outage(supervisor)
    assert supervisor._watchdog_should_bounce(time.time()) is False


def test_bounce_after_the_threshold(supervisor):
    """The assertion that would have ended the 72-hour outage at minute 15."""
    supervisor._proc = LiveProc()
    _drive_into_outage(supervisor)
    assert supervisor._watchdog_should_bounce(_after_threshold()) is True


def test_a_dead_child_is_the_supervisors_job_not_the_watchdogs(supervisor):
    supervisor._proc = ExitedProc()
    _drive_into_outage(supervisor)
    assert supervisor._watchdog_should_bounce(_after_threshold()) is False


def test_a_boot_that_never_connects_still_gets_bounced(supervisor):
    """No up marker ever — connected_at None plus one failure reads as False."""
    supervisor._proc = LiveProc()
    supervisor._note_gateway_line(RETRY_LINE)
    assert supervisor._socket_state() is False
    assert supervisor._socket_down_since is not None
    assert supervisor._watchdog_should_bounce(_after_threshold()) is True


def test_the_streak_cap_stops_the_bounce_loop(supervisor):
    """A revoked token cannot be bounced away; the watchdog must say so and stop."""
    supervisor._proc = LiveProc()
    _drive_into_outage(supervisor)
    supervisor._watchdog_bounces_consecutive = (
        health_gateway.SOCKET_WATCHDOG_MAX_CONSECUTIVE
    )
    assert supervisor._watchdog_should_bounce(_after_threshold()) is False
    assert supervisor.snapshot()["slack_socket_watchdog"]["exhausted"] is True


def test_a_reconnect_clears_the_clock_and_the_streak(supervisor):
    _drive_into_outage(supervisor)
    supervisor._watchdog_bounces_consecutive = 2
    supervisor._note_gateway_line(CONNECT_LINE)
    assert supervisor._socket_down_since is None
    watchdog = supervisor.snapshot()["slack_socket_watchdog"]
    assert watchdog["bounces_consecutive"] == 0
    assert watchdog["exhausted"] is False


def test_disabling_the_watchdog_disables_the_bounce(supervisor, monkeypatch):
    monkeypatch.setattr(health_gateway, "SOCKET_DEAD_RESTART_SECONDS", 0)
    supervisor._proc = LiveProc()
    _drive_into_outage(supervisor)
    assert supervisor._watchdog_should_bounce(time.time() + 10_000) is False
    assert supervisor.snapshot()["slack_socket_watchdog"]["enabled"] is False


def _degraded_health_payload(monkeypatch, supervisor):
    """The /health ladder with the given supervisor's real snapshot, Cleo's BOOT."""
    monkeypatch.setattr(health_gateway, "GATEWAY_ENABLED", True)
    monkeypatch.setattr(
        health_gateway,
        "BOOT",
        {"agent": "cleo", "model_provider": "xai-oauth", "subscription_auth": {"logged_in": True}},
    )
    monkeypatch.setattr(
        type(supervisor), "slack_adapter_available", property(lambda self: True)
    )
    monkeypatch.setattr(type(supervisor), "mcp_sdk_available", property(lambda self: True))
    monkeypatch.setattr(type(supervisor), "cli_present", property(lambda self: True))
    snap = supervisor.snapshot()
    snap["running"] = True
    monkeypatch.setattr(health_gateway, "supervisor", supervisor)
    monkeypatch.setattr(supervisor, "snapshot", lambda: snap)
    return health_gateway.health()


def test_health_note_names_the_watchdog_plan(monkeypatch):
    """A degraded socket must tell the reader repair is already scheduled."""
    supervisor = health_gateway.GatewaySupervisor()
    _drive_into_outage(supervisor)
    payload = _degraded_health_payload(monkeypatch, supervisor)
    assert payload["mode"] == "gateway_slack_socket_down"
    assert "watchdog will bounce the gateway" in payload["note"]


def test_health_note_says_when_the_watchdog_gave_up(monkeypatch):
    supervisor = health_gateway.GatewaySupervisor()
    _drive_into_outage(supervisor)
    supervisor._watchdog_bounces_consecutive = (
        health_gateway.SOCKET_WATCHDOG_MAX_CONSECUTIVE
    )
    payload = _degraded_health_payload(monkeypatch, supervisor)
    assert payload["mode"] == "gateway_slack_socket_down"
    assert "given up" in payload["note"]
