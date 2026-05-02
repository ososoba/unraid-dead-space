"""SQLite upsert + tombstone helpers.

`upsert(conn, table, row, conflict_keys, update_columns)` issues an
`INSERT ... ON CONFLICT(...) DO UPDATE SET ...` statement and returns
the lastrowid (which for an UPDATE is the existing row's id).

`mark_tombstones(conn, table, run_id, scope_clause, scope_params)` flips
`deleted_at` on rows whose `last_seen_sync_run_id` does not match the
current run, scoped (e.g. to one Arr instance) so a partial pull does
not delete rows from other instances.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def upsert(
    conn: sqlite3.Connection,
    table: str,
    row: Mapping[str, object],
    *,
    conflict_keys: Sequence[str],
    update_columns: Sequence[str] | None = None,
) -> int:
    """INSERT ... ON CONFLICT DO UPDATE. Returns the row id (insert or existing).

    `conflict_keys` is the column tuple in a UNIQUE constraint.
    `update_columns` defaults to "every column not in conflict_keys".
    """
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(columns)
    update_cols = (
        list(update_columns)
        if update_columns is not None
        else [c for c in columns if c not in set(conflict_keys)]
    )
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols) if update_cols else ""
    conflict_target = ", ".join(conflict_keys)
    if set_clause:
        sql = (
            f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_target}) DO UPDATE SET {set_clause} "
            f"RETURNING id"
        )
    else:
        sql = (
            f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_target}) DO NOTHING RETURNING id"
        )

    cur = conn.execute(sql, [row[c] for c in columns])
    rid = cur.fetchone()
    if rid is not None:
        return int(rid[0])

    # ON CONFLICT DO NOTHING with no RETURNING row — fetch the existing id.
    where = " AND ".join(f"{k} = ?" for k in conflict_keys)
    existing = conn.execute(
        f"SELECT id FROM {table} WHERE {where}",
        [row[k] for k in conflict_keys],
    ).fetchone()
    return int(existing[0]) if existing else 0


def mark_tombstones(
    conn: sqlite3.Connection,
    table: str,
    *,
    run_id: int,
    scope_clause: str = "1=1",
    scope_params: Sequence[object] = (),
) -> int:
    """Soft-delete rows missed by this run.

    Sets `deleted_at = NOW()` where `last_seen_sync_run_id != run_id` AND
    `deleted_at IS NULL` AND <scope_clause>. Returns rows affected.
    """
    with conn:
        cur = conn.execute(
            f"UPDATE {table} SET deleted_at = ? "
            f"WHERE last_seen_sync_run_id IS NOT NULL "
            f"  AND last_seen_sync_run_id != ? "
            f"  AND deleted_at IS NULL "
            f"  AND {scope_clause}",
            [_now_iso(), run_id, *scope_params],
        )
    return cur.rowcount


def revive(conn: sqlite3.Connection, table: str, row_id: int) -> None:
    """Clear the tombstone on a row (used when an item reappears in a later sync)."""
    with conn:
        conn.execute(
            f"UPDATE {table} SET deleted_at = NULL WHERE id = ?",
            (row_id,),
        )
