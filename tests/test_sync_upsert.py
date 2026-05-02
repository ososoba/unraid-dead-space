"""upsert + tombstone behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from dms.db import connect
from dms.sync.upsert import mark_tombstones, upsert


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "db.sqlite")
    c.executescript(
        """
        CREATE TABLE thing (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          natural_key TEXT NOT NULL UNIQUE,
          value TEXT,
          last_seen_sync_run_id INTEGER,
          deleted_at TEXT
        );
        """
    )
    yield c
    c.close()


def test_upsert_inserts_new_row(conn) -> None:
    rid = upsert(
        conn,
        "thing",
        {"natural_key": "k1", "value": "first", "last_seen_sync_run_id": 1, "deleted_at": None},
        conflict_keys=("natural_key",),
    )
    assert rid == 1


def test_upsert_updates_existing_row(conn) -> None:
    upsert(
        conn,
        "thing",
        {"natural_key": "k1", "value": "first", "last_seen_sync_run_id": 1, "deleted_at": None},
        conflict_keys=("natural_key",),
    )
    rid = upsert(
        conn,
        "thing",
        {"natural_key": "k1", "value": "second", "last_seen_sync_run_id": 2, "deleted_at": None},
        conflict_keys=("natural_key",),
    )
    # Same id, updated value.
    assert rid == 1
    row = conn.execute("SELECT value, last_seen_sync_run_id FROM thing").fetchone()
    assert row["value"] == "second"
    assert row["last_seen_sync_run_id"] == 2


def test_mark_tombstones_flips_missed_rows(conn) -> None:
    upsert(
        conn,
        "thing",
        {"natural_key": "k1", "value": "x", "last_seen_sync_run_id": 1, "deleted_at": None},
        conflict_keys=("natural_key",),
    )
    upsert(
        conn,
        "thing",
        {"natural_key": "k2", "value": "y", "last_seen_sync_run_id": 2, "deleted_at": None},
        conflict_keys=("natural_key",),
    )
    # Run 2 — k1 was missed, k2 was seen.
    rows_changed = mark_tombstones(conn, "thing", run_id=2)
    assert rows_changed == 1
    row1 = conn.execute("SELECT deleted_at FROM thing WHERE natural_key = 'k1'").fetchone()
    row2 = conn.execute("SELECT deleted_at FROM thing WHERE natural_key = 'k2'").fetchone()
    assert row1["deleted_at"] is not None
    assert row2["deleted_at"] is None


def test_mark_tombstones_idempotent(conn) -> None:
    upsert(
        conn,
        "thing",
        {"natural_key": "k1", "value": "x", "last_seen_sync_run_id": 1, "deleted_at": None},
        conflict_keys=("natural_key",),
    )
    first = mark_tombstones(conn, "thing", run_id=2)
    second = mark_tombstones(conn, "thing", run_id=2)
    assert first == 1
    assert second == 0  # already tombstoned


def test_mark_tombstones_respects_scope(conn) -> None:
    conn.execute("ALTER TABLE thing ADD COLUMN scope_id INTEGER")
    conn.commit()
    conn.execute(
        "INSERT INTO thing (natural_key, value, last_seen_sync_run_id, scope_id) "
        "VALUES ('a', 'A', 1, 1), ('b', 'B', 1, 2)"
    )
    conn.commit()

    # Tombstone only scope_id=1.
    rows = mark_tombstones(
        conn,
        "thing",
        run_id=2,
        scope_clause="scope_id = ?",
        scope_params=(1,),
    )
    assert rows == 1
    a = conn.execute("SELECT deleted_at FROM thing WHERE natural_key = 'a'").fetchone()
    b = conn.execute("SELECT deleted_at FROM thing WHERE natural_key = 'b'").fetchone()
    assert a["deleted_at"] is not None
    assert b["deleted_at"] is None
