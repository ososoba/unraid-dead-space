"""Connection helper sanity checks."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dms.db import connect, connection_scope


def test_connect_creates_parent_dir(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "dir" / "db.sqlite"
    conn = connect(db)
    try:
        assert db.parent.is_dir()
        assert db.exists()
    finally:
        conn.close()


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        cur = conn.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_wal_mode_set_for_disk_db(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        cur = conn.execute("PRAGMA journal_mode")
        assert cur.fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_row_factory_is_dict_like(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        conn.execute("CREATE TABLE x (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO x VALUES (1, 'hi')")
        row = conn.execute("SELECT a, b FROM x").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["a"] == 1
        assert row["b"] == "hi"
    finally:
        conn.close()


def test_connection_scope_closes(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    with connection_scope(db) as conn:
        conn.execute("CREATE TABLE y (a INTEGER)")
    # Re-open to confirm earlier conn is closed (no lock held)
    conn2 = sqlite3.connect(db, timeout=0.5)
    conn2.execute("INSERT INTO y VALUES (1)")
    conn2.close()


def test_in_memory_db_skips_wal(tmp_path: Path) -> None:
    # WAL is not supported on :memory: — connect should not attempt to set it.
    conn = connect(":memory:")
    try:
        cur = conn.execute("PRAGMA journal_mode")
        # default is "memory" for in-memory DBs; we only verify no error
        assert cur.fetchone() is not None
    finally:
        conn.close()


def test_foreign_key_violation_raises(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        conn.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE c (pid INTEGER NOT NULL REFERENCES p(id))")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO c (pid) VALUES (999)")
            conn.commit()
    finally:
        conn.close()
