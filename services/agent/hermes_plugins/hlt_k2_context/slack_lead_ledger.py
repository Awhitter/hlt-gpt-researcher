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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS slack_thread_participants (
                workspace_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                agent_refs_json TEXT NOT NULL,
                mention_message_ts TEXT NOT NULL,
                updated_at_unix REAL NOT NULL,
                PRIMARY KEY (workspace_id, channel_id, thread_ts)
            )
            """
        )
        return connection

    def thread_participants(
        self, *, workspace_id: str, channel_id: str, thread_ts: str
    ) -> tuple[str, ...]:
        """Return the latest human-invited participant set for one thread."""
        if not workspace_id or not channel_id or not thread_ts:
            return ()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT agent_refs_json
                FROM slack_thread_participants
                WHERE workspace_id = ? AND channel_id = ? AND thread_ts = ?
                """,
                (workspace_id, channel_id, thread_ts),
            ).fetchone()
        if row is None:
            return ()
        decoded = json.loads(str(row["agent_refs_json"] or "[]"))
        if not isinstance(decoded, list) or not all(
            isinstance(value, str) and value.startswith("agent:")
            for value in decoded
        ):
            raise ValueError("stored thread participants have an invalid shape")
        return tuple(dict.fromkeys(decoded))

    def assign_thread_participants(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        thread_ts: str,
        agent_refs: tuple[str, ...],
        mention_message_ts: str,
    ) -> None:
        """Persist the latest explicit human invitation set for follow-ups.

        Slack timestamps are fixed-width epoch-second strings in this
        workspace, so lexical order is chronological. The conditional upsert
        prevents a late Socket Mode retry from rolling participation backward.
        A later human mention replaces (and therefore may narrow) the set.
        Bot messages never call this method.
        """
        normalized = tuple(
            dict.fromkeys(str(value or "").strip().lower() for value in agent_refs)
        )
        if not all((workspace_id, channel_id, thread_ts, mention_message_ts)):
            raise ValueError("workspace/channel/thread/message ts are required")
        if not normalized or not all(value.startswith("agent:") for value in normalized):
            raise ValueError("at least one valid agent participant is required")
        encoded = json.dumps(normalized, separators=(",", ":"))
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO slack_thread_participants (
                    workspace_id, channel_id, thread_ts, agent_refs_json,
                    mention_message_ts, updated_at_unix
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, channel_id, thread_ts) DO UPDATE SET
                    agent_refs_json = excluded.agent_refs_json,
                    mention_message_ts = excluded.mention_message_ts,
                    updated_at_unix = excluded.updated_at_unix
                WHERE excluded.mention_message_ts >= slack_thread_participants.mention_message_ts
                """,
                (
                    workspace_id,
                    channel_id,
                    thread_ts,
                    encoded,
                    mention_message_ts,
                    now,
                ),
            )
            connection.commit()

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
