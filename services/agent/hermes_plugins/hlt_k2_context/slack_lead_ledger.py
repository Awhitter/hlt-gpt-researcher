"""Durable, local at-most-once tombstones for Slack lead decisions."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "slack_agent_lead_decision.v1"
RETENTION_SECONDS = 45 * 24 * 60 * 60


@dataclass(frozen=True)
class TombstoneResult:
    inserted: bool
    receipt: dict[str, Any]


class SlackLeadLedger:
    """A tiny SQLite ledger stored on the agent's durable Hermes disk."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS slack_agent_lead_tombstones (
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_ts TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                created_at_unix REAL NOT NULL,
                PRIMARY KEY (workspace_id, channel_id, message_ts)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS slack_agent_lead_created_idx
            ON slack_agent_lead_tombstones (created_at_unix)
            """
        )
        return connection

    def record_once(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        message_ts: str,
        receipt: dict[str, Any],
    ) -> TombstoneResult:
        """Atomically record the first decision; every replay is suppressed."""
        if not workspace_id or not channel_id or not message_ts:
            raise ValueError("workspace/channel/message ts are required")
        if receipt.get("schema") != RECEIPT_SCHEMA:
            raise ValueError(f"receipt schema must be {RECEIPT_SCHEMA}")

        now = time.time()
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT receipt_json
                FROM slack_agent_lead_tombstones
                WHERE workspace_id = ? AND channel_id = ? AND message_ts = ?
                """,
                (workspace_id, channel_id, message_ts),
            ).fetchone()
            if existing is not None:
                decoded = json.loads(str(existing["receipt_json"]))
                if not isinstance(decoded, dict) or decoded.get("schema") != RECEIPT_SCHEMA:
                    raise ValueError("stored lead tombstone has an invalid receipt")
                connection.commit()
                return TombstoneResult(
                    inserted=False,
                    receipt=decoded,
                )
            connection.execute(
                """
                INSERT INTO slack_agent_lead_tombstones (
                    workspace_id, channel_id, message_ts,
                    receipt_json, created_at_unix
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (workspace_id, channel_id, message_ts, encoded, now),
            )
            connection.execute(
                """
                DELETE FROM slack_agent_lead_tombstones
                WHERE created_at_unix < ?
                """,
                (now - RETENTION_SECONDS,),
            )
            connection.commit()
        return TombstoneResult(inserted=True, receipt=receipt)
