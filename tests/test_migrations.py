"""Migration runner + initial schema tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dms.db import connect
from dms.migrations import applied_versions, apply_pending, list_migrations

EXPECTED_TABLES = {
    "instances",
    "arr_items",
    "arr_episodes",
    "arr_files",
    "plex_items",
    "plex_media_files",
    "watch_events",
    "tags",
    "requests",
    "request_attribution",
    "user_identity_map",
    "watch_state",
    "candidates",
    "ignore_rules",
    "sync_jobs",
    "sync_run_steps",
    "sync_locks",
    "audit_log",
    "config",
    "schema_version",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in cur.fetchall() if not row[0].startswith("sqlite_")}


def test_list_migrations_finds_initial() -> None:
    migs = list_migrations()
    assert any(m.version == 1 and m.name == "initial" for m in migs)


def test_apply_pending_creates_all_expected_tables(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        applied = apply_pending(conn)
        # Whatever migrations exist, they should be applied in order.
        assert [m.version for m in applied] == sorted(m.version for m in applied)
        assert EXPECTED_TABLES.issubset(_table_names(conn))
    finally:
        conn.close()


def test_apply_pending_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        first = apply_pending(conn)
        second = apply_pending(conn)
        assert first  # at least one migration
        assert second == []
        assert applied_versions(conn) == {m.version for m in first}
    finally:
        conn.close()


def test_schema_version_records_description(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_pending(conn)
        row = conn.execute(
            "SELECT version, description FROM schema_version ORDER BY version"
        ).fetchone()
        assert row["version"] == 1
        assert row["description"] == "initial"
    finally:
        conn.close()


def test_candidates_check_constraint_blocks_double_null(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_pending(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO candidates
                  (arr_item_id, plex_item_id, reason, scope, confidence,
                   computed_at_sync_run_id)
                VALUES (NULL, NULL, 'orphan_arr_no_plex', 'anyone', 'high', 1)
                """
            )
            conn.commit()
    finally:
        conn.close()


def test_candidates_accepts_arr_only(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_pending(conn)
        conn.execute(
            "INSERT INTO instances (kind, slug, name, url, api_key) "
            "VALUES ('radarr', 'radarr-1', 'R', 'http://x', 'k')"
        )
        conn.execute(
            "INSERT INTO arr_items (instance_id, kind, arr_id, title) "
            "VALUES (1, 'movie', 42, 'Test')"
        )
        conn.execute(
            """
            INSERT INTO candidates
              (arr_item_id, plex_item_id, reason, scope, confidence,
               computed_at_sync_run_id, size_bytes)
            VALUES (1, NULL, 'never_watched_anyone', 'anyone', 'high', 1, 1024)
            """
        )
        conn.commit()
        row = conn.execute("SELECT arr_item_id, reason FROM candidates").fetchone()
        assert row["arr_item_id"] == 1
        assert row["reason"] == "never_watched_anyone"
    finally:
        conn.close()


def test_instances_kind_check_constraint(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_pending(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO instances (kind, slug, name, url, api_key) "
                "VALUES ('plex', 'p1', 'P', 'http://x', 'k')"
            )
            conn.commit()
    finally:
        conn.close()


def test_cascade_delete_arr_item_drops_episodes(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_pending(conn)
        conn.execute(
            "INSERT INTO instances (kind, slug, name, url, api_key) "
            "VALUES ('sonarr', 'sonarr-1', 'S', 'http://x', 'k')"
        )
        conn.execute(
            "INSERT INTO arr_items (instance_id, kind, arr_id, title) "
            "VALUES (1, 'series', 7, 'Show')"
        )
        conn.execute(
            "INSERT INTO arr_episodes "
            "(instance_id, arr_item_id, arr_episode_id, season_number, episode_number) "
            "VALUES (1, 1, 100, 1, 1)"
        )
        conn.commit()

        conn.execute("DELETE FROM arr_items WHERE id = 1")
        conn.commit()
        cnt = conn.execute("SELECT COUNT(*) FROM arr_episodes").fetchone()[0]
        assert cnt == 0
    finally:
        conn.close()


def test_unique_arr_id_per_instance(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_pending(conn)
        conn.execute(
            "INSERT INTO instances (kind, slug, name, url, api_key) "
            "VALUES ('radarr', 'radarr-1', 'R', 'http://x', 'k')"
        )
        conn.execute(
            "INSERT INTO arr_items (instance_id, kind, arr_id, title) VALUES (1, 'movie', 99, 'A')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO arr_items (instance_id, kind, arr_id, title) "
                "VALUES (1, 'movie', 99, 'B')"
            )
            conn.commit()
    finally:
        conn.close()
