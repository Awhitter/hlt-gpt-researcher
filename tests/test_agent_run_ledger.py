"""Crash-window contracts for Cleo's durable external-run admission ledger."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "services" / "agent" / "agent_run_ledger.py"
)
SPEC = importlib.util.spec_from_file_location(
    "test_agent_run_ledger_module", MODULE_PATH
)
ledger_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger_module)

K2_RUN_ID = "11111111-1111-4111-8111-111111111111"
SESSION_KEY = f"hook:k2:{K2_RUN_ID}"
WRAPPER_RUN_ID = "run_11111111111141118111111111111111"


def _admit(store, *, fingerprint="sha256:one", session_key=SESSION_KEY):
    return store.admit(
        k2_run_id=K2_RUN_ID,
        session_key=session_key,
        org_id="org-123",
        agent_ref="agent:cleo",
        fingerprint=fingerprint,
    )


def test_wrapper_run_id_is_the_exact_k2_uuid_without_hyphens():
    assert ledger_module.wrapper_run_id(K2_RUN_ID) == WRAPPER_RUN_ID
    with pytest.raises(ValueError, match="canonical lowercase UUID"):
        ledger_module.wrapper_run_id("run-123")


def test_exact_replay_survives_a_new_store_instance(tmp_path):
    path = tmp_path / "agent-runs.sqlite3"
    first = ledger_module.AgentRunLedger(path)
    admitted, created = _admit(first)
    restarted = ledger_module.AgentRunLedger(path)
    replay, replay_created = _admit(restarted)

    assert created is True
    assert replay_created is False
    assert admitted["wrapper_run_id"] == WRAPPER_RUN_ID
    assert replay == admitted


def test_run_or_session_reuse_with_changed_semantics_is_rejected(tmp_path):
    store = ledger_module.AgentRunLedger(tmp_path / "agent-runs.sqlite3")
    _admit(store)

    with pytest.raises(ledger_module.AdmissionConflict):
        _admit(store, fingerprint="sha256:different")
    with pytest.raises(ledger_module.AdmissionConflict):
        store.admit(
            k2_run_id="22222222-2222-4222-8222-222222222222",
            session_key=SESSION_KEY,
            org_id="org-123",
            agent_ref="agent:cleo",
            fingerprint="sha256:two",
        )


def test_dispatch_claim_is_one_way_and_provider_binding_is_durable(tmp_path):
    path = tmp_path / "agent-runs.sqlite3"
    store = ledger_module.AgentRunLedger(path)
    _admit(store)

    assert store.claim_dispatch(WRAPPER_RUN_ID) is True
    assert store.claim_dispatch(WRAPPER_RUN_ID) is False
    assert store.get(WRAPPER_RUN_ID)["admission_status"] == "dispatching"

    store.bind_provider(WRAPPER_RUN_ID, "run_" + "a" * 32)
    restarted = ledger_module.AgentRunLedger(path)
    record = restarted.get(WRAPPER_RUN_ID)
    assert record["admission_status"] == "provider_bound"
    assert record["provider_run_id"] == "run_" + "a" * 32


def test_terminal_output_is_bounded_and_secret_redacted(tmp_path):
    store = ledger_module.AgentRunLedger(tmp_path / "agent-runs.sqlite3")
    _admit(store)
    store.claim_dispatch(WRAPPER_RUN_ID)
    store.bind_provider(WRAPPER_RUN_ID, "run_" + "b" * 32)
    secret = "sk-or-v1-" + "z" * 80

    store.mark_terminal(
        WRAPPER_RUN_ID,
        "completed",
        output=f"result {secret} " + "x" * 60_000,
        error=f"access_token={secret}",
        usage={"total_tokens": 41, "refresh_token": secret},
    )
    record = store.get(WRAPPER_RUN_ID)

    assert record["admission_status"] == "terminal"
    assert record["provider_status"] == "completed"
    assert secret not in record["output_text"]
    assert len(record["output_text"]) <= ledger_module.MAX_OUTPUT_CHARS
    assert record["usage"] == {"total_tokens": 41, "refresh_token": "[redacted]"}


def test_every_write_connection_carries_full_synchronous(tmp_path):
    """Durability is per-connection: WAL persists in the DB header but
    PRAGMA synchronous does not. The docstring's no-double-dispatch promise
    holds only if the connections that actually WRITE (admit/claim) run at
    FULL — not just the throwaway _initialize() connection."""
    store = ledger_module.AgentRunLedger(tmp_path / "ledger.sqlite")
    conn = store._connect()
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    finally:
        conn.close()
