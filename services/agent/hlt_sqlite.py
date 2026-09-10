"""Journal and durability policy for Cleo's two HLT-owned SQLite ledgers."""

from __future__ import annotations

import sqlite3


def wal_reset_fixed(version: tuple[int, ...]) -> bool:
    """Official fixed releases, including the two maintained backports.

    https://sqlite.org/wal.html#walresetbug
    An unqualified distro backport is deliberately not inferred from its name.
    """
    return (
        version >= (3, 51, 3)
        or (3, 50, 7) <= version < (3, 51, 0)
        or (3, 44, 6) <= version < (3, 45, 0)
    )


def configure_journal(connection: sqlite3.Connection) -> str:
    """Keep journal transitions safe and every write fully durable.

    The image ships a fixed SQLite. Older local runtimes may use an existing
    rollback journal, but cannot open an existing WAL ledger for application
    work. Never downgrade WAL while another process may hold committed frames.
    Failed/locked mode reads propagate; unknown is never treated as DELETE.
    """
    mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if wal_reset_fixed(sqlite3.sqlite_version_info):
        if mode != "wal":
            mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
    elif mode == "wal":
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} cannot safely use this WAL ledger; "
            "upgrade the Python-linked SQLite runtime before reopening it. "
            "The existing journal has not been changed."
        )
    elif mode not in {"delete", "truncate", "persist"}:
        raise RuntimeError(f"Unsupported durable SQLite journal mode: {mode}")
    if mode not in {"wal", "delete", "truncate", "persist"}:
        raise RuntimeError(f"SQLite did not enable a durable journal: {mode}")
    connection.execute("PRAGMA synchronous=FULL")
    return mode
