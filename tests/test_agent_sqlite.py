"""Real journal, restart, and at-most-once behavior of both HLT stores."""

import importlib.util
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

import hlt_sqlite

SERVICE = Path(__file__).resolve().parents[1] / "services/agent"


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, SERVICE / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.fixture(params=["run", "lead"])
def store_type(request):
    if request.param == "run":
        return _load("sqlite_run_test", "agent_run_ledger.py").AgentRunLedger
    return _load(
        "sqlite_lead_test", "hermes_plugins/hlt_k2_context/slack_lead_ledger.py"
    ).SlackLeadLedger


@pytest.mark.parametrize("version,fixed", [
    ((3, 40, 1), False), ((3, 44, 5), False), ((3, 44, 6), True),
    ((3, 45, 0), False), ((3, 50, 6), False), ((3, 50, 7), True),
    ((3, 51, 0), False), ((3, 51, 2), False), ((3, 51, 3), True),
    ((3, 53, 4), True),
])
def test_official_fixed_release_boundaries(version, fixed):
    assert hlt_sqlite.wal_reset_fixed(version) is fixed


def test_old_runtime_does_not_enable_wal(store_type, tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 40, 1))
    store = store_type(tmp_path / "fresh.db")
    with closing(store._connect()) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_existing_wal_is_never_downgraded_with_a_live_writer(
    store_type, tmp_path, monkeypatch
):
    path = tmp_path / "retained.db"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE retained (value INTEGER)")
        writer.execute("INSERT INTO retained VALUES (42)")
        writer.commit()
        monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 40, 1))
        with pytest.raises(RuntimeError, match="upgrade the Python-linked"):
            with closing(store_type(path)._connect()):
                pass
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert writer.execute("SELECT value FROM retained").fetchall() == [(42,)]
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("SELECT value FROM retained").fetchall() == [(42,)]


def test_unreadable_mode_propagates_without_a_journal_write(tmp_path):
    class LockedRead(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA journal_mode":
                raise sqlite3.OperationalError("database is locked")
            if "journal_mode=" in sql:
                pytest.fail("An unknown journal mode must not be changed")
            return super().execute(sql, *args, **kwargs)

    with closing(sqlite3.connect(tmp_path / "locked.db", factory=LockedRead)) as conn:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            hlt_sqlite.configure_journal(conn)


def test_ledger_operations_close_their_owned_connections(store_type, tmp_path, monkeypatch):
    opened = []

    class ObservedConnection(sqlite3.Connection):
        was_closed = False

        def close(self):
            self.was_closed = True
            super().close()

    original = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs, factory=ObservedConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    store = store_type(tmp_path / "closed.db")
    if hasattr(store, "probe"):
        store.probe()
    else:
        store.thread_participants(workspace_id="T", channel_id="C", thread_ts="1")
    assert opened and all(connection.was_closed for connection in opened)


def test_schema_failure_closes_the_connection(store_type, tmp_path, monkeypatch):
    opened = []

    class FailedSchema(sqlite3.Connection):
        was_closed = False

        def execute(self, sql, *args, **kwargs):
            if "CREATE TABLE" in sql:
                raise sqlite3.OperationalError("schema setup failed")
            return super().execute(sql, *args, **kwargs)

        def executescript(self, sql, *args, **kwargs):
            raise sqlite3.OperationalError("schema setup failed")

        def close(self):
            self.was_closed = True
            super().close()

    original = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs, factory=FailedSchema)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="schema setup failed"):
        store_type(tmp_path / "failed.db")._connect()
    assert opened and all(connection.was_closed for connection in opened)
