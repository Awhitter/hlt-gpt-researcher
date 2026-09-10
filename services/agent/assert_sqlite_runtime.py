"""Offline build gate: actual linked SQLite and both durable HLT consumers."""

import importlib.util
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_run_ledger import AgentRunLedger, wrapper_run_id

EXPECTED_SOURCE = "2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc"


def main():
    assert sqlite3.sqlite_version == "3.53.4", sqlite3.sqlite_version
    assert sqlite3.threadsafety == 3, sqlite3.threadsafety
    with closing(sqlite3.connect(":memory:")) as connection:
        assert connection.execute("SELECT sqlite_source_id()").fetchone()[0] == EXPECTED_SOURCE
        assert connection.execute("SELECT json_extract('{\"value\":42}', '$.value')").fetchone()[0] == 42
        connection.execute("CREATE VIRTUAL TABLE search USING fts5(body)")
        connection.execute("INSERT INTO search VALUES ('nurse recruiting')")
        assert connection.execute("SELECT body FROM search WHERE search MATCH 'nurse'").fetchone()[0] == "nurse recruiting"
        connection.execute("CREATE VIRTUAL TABLE area USING rtree(id, min_x, max_x)")

    # Load only the ledger, without starting the plugin, gateway, or a provider.
    path = Path(__file__).parent / "hermes_plugins/hlt_k2_context/slack_lead_ledger.py"
    spec = importlib.util.spec_from_file_location("hlt_sqlite_lead_assertion", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        with TemporaryDirectory(prefix="hlt-sqlite-proof-") as directory:
            run_path, lead_path = Path(directory) / "runs.db", Path(directory) / "leads.db"
            run_id = "11111111-1111-4111-8111-111111111111"
            runs = AgentRunLedger(run_path)
            runs.admit(k2_run_id=run_id, session_key=f"hook:k2:{run_id}", org_id="proof",
                       agent_ref="agent:cleo", fingerprint="sha256:sqlite-offline-proof")
            with ThreadPoolExecutor(max_workers=4) as pool:
                claims = list(pool.map(lambda _: AgentRunLedger(run_path).claim_dispatch(wrapper_run_id(run_id)), range(8)))
            assert claims.count(True) == 1, claims
            assert AgentRunLedger(run_path).get(wrapper_run_id(run_id))["admission_status"] == "dispatching"
            with closing(module.SlackLeadLedger(lead_path)._connect()):
                pass
            receipt = {"schema": module.RECEIPT_SCHEMA, "action": "allow", "reason": "offline-proof"}
            def record(_):
                return module.SlackLeadLedger(lead_path).record_once(
                    workspace_id="T", channel_id="C", message_ts="1", receipt=receipt
                ).inserted
            with ThreadPoolExecutor(max_workers=4) as pool:
                decisions = list(pool.map(record, range(8)))
            assert decisions.count(True) == 1, decisions
            assert record(None) is False
            for store in (AgentRunLedger(run_path), module.SlackLeadLedger(lead_path)):
                with closing(store._connect()) as connection:
                    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
                    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
                    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        sys.modules.pop(spec.name, None)
    print(json.dumps({"sqlite": sqlite3.sqlite_version, "sourceId": EXPECTED_SOURCE,
                      "hltLedgers": "PASS", "concurrentAtMostOnce": "PASS", "providerCalls": 0}))


if __name__ == "__main__":
    main()
