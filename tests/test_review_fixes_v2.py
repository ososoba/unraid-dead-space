"""Coverage for the second Codex review pass: series file-level orphans,
XFF spoof in login throttle, 24h overlap, multi-part Plex media files."""

from __future__ import annotations

from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from dms.app import create_app
from dms.db import connect
from dms.migrations import apply_pending
from dms.routes import login as login_module
from dms.sync.candidates_db import compute_candidates
from dms.sync.plex_sync import PlexFileMeta, _upsert_plex_media_file
from dms.sync.tautulli_sync import _incremental_after, _max_row_id
from tests.conftest import login_with_csrf

TEST_PASSWORD = "hunter2-correct-horse"
TEST_USERNAME = "admin"


# ---------- #1: Series file-level orphan detection ----------


def _seed_series_orphan_fixture(db: Path) -> None:
    """Two Sonarr instances with the same show, different total sizes (split
    quality). Plex has only the 4K version; expect the 1080p instance to
    flag as orphan even though tvdb_id matches Plex."""
    c = connect(db)
    apply_pending(c)
    c.executescript(
        """
        INSERT INTO instances (id, kind, slug, name, url, api_key)
        VALUES (1, 'sonarr', 'sonarr-1080', 'Sonarr 1080p', 'http://x', 'k'),
               (2, 'sonarr', 'sonarr-4k',   'Sonarr 4K',    'http://x', 'k');
        INSERT INTO arr_items (id, instance_id, kind, arr_id, title, tvdb_id,
                               last_seen_sync_run_id)
        VALUES
          (1, 1, 'series', 100, 'Show X', 777, 1),
          (2, 2, 'series', 100, 'Show X', 777, 1);
        -- 1080p: 50GB total across episodes
        -- 4K:    500GB total across episodes
        INSERT INTO arr_files (instance_id, arr_item_id, kind, arr_file_id,
                               size_bytes, last_seen_sync_run_id)
        VALUES
          (1, 1, 'episode', 1001, 25000000000, 1),
          (1, 1, 'episode', 1002, 25000000000, 1),
          (2, 2, 'episode', 2001, 250000000000, 1),
          (2, 2, 'episode', 2002, 250000000000, 1);
        INSERT INTO plex_items (id, rating_key, kind, title, tvdb_id,
                                last_seen_sync_run_id)
        VALUES (1, 9999, 'show', 'Show X', 777, 1);
        -- Plex has only the 4K-equivalent (~500GB total).
        INSERT INTO plex_media_files (plex_item_id, rating_key, part_index, size_bytes,
                                      last_seen_sync_run_id)
        VALUES
          (1, 9999, 0, 250000000000, 1),
          (1, 9999, 1, 250000000000, 1);
        INSERT INTO sync_jobs (id, kind, status, started_at)
        VALUES (1, 'manual', 'succeeded', datetime('now'));
        """
    )
    c.close()


def test_series_orphan_flags_unmatched_quality(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    _seed_series_orphan_fixture(db)
    c = connect(db)
    try:
        compute_candidates(c, run_id=1)
        rows = c.execute(
            """SELECT i.slug, c.size_bytes
               FROM candidates c
               JOIN arr_items ai ON ai.id = c.arr_item_id
               JOIN instances i ON i.id = ai.instance_id
               WHERE c.reason = 'orphan_arr_no_plex'
               ORDER BY c.size_bytes"""
        ).fetchall()
    finally:
        c.close()
    slugs = [r["slug"] for r in rows]
    # Only the 1080p Sonarr instance is orphan; 4K matches Plex by size.
    assert slugs == ["sonarr-1080"]


def test_series_orphan_quiet_when_plex_has_no_size_data(tmp_path: Path) -> None:
    """If Plex has the show but no media files (e.g. fresh, unrefreshed),
    we keep the previous lenient behavior — don't flag as orphan."""
    db = tmp_path / "db.sqlite"
    _seed_series_orphan_fixture(db)
    c = connect(db)
    try:
        c.execute("UPDATE plex_media_files SET size_bytes = 0")
        c.commit()
        compute_candidates(c, run_id=1)
        n = c.execute(
            "SELECT COUNT(*) FROM candidates WHERE reason = 'orphan_arr_no_plex'"
        ).fetchone()[0]
    finally:
        c.close()
    assert n == 0


# ---------- #2: Login throttle XFF spoof ----------


def test_login_client_id_uses_request_client_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The throttle must NOT trust raw X-Forwarded-For — that would let any
    LAN client rotate identities and dodge the lockout."""
    monkeypatch.setenv("APP_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
    )
    monkeypatch.setenv("SESSION_SECRET", "test-secret-key-must-be-at-least-16-chars")
    monkeypatch.setenv("COOKIE_SECURE", "false")

    # Build a stub Request-like object the unit can introspect.
    class _Stub:
        def __init__(self, host: str, headers: dict[str, str]):
            self.client = type("C", (), {"host": host})()
            self.headers = headers

    spoofed = _Stub("10.0.0.5", {"x-forwarded-for": "1.2.3.4"})
    real_only = _Stub("10.0.0.5", {})
    assert login_module._client_id(spoofed) == "10.0.0.5"
    assert login_module._client_id(real_only) == "10.0.0.5"


def test_throttle_locks_same_client_despite_xff_spoofing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five bad attempts from the same socket peer should lock the next one,
    even if every attempt sent a different X-Forwarded-For header."""
    monkeypatch.setenv("APP_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
    )
    monkeypatch.setenv("SESSION_SECRET", "test-secret-key-must-be-at-least-16-chars")
    monkeypatch.setenv("COOKIE_SECURE", "false")

    app = create_app(db_path=tmp_path / "db.sqlite", enable_scheduler=False)
    with TestClient(app) as client:
        for i in range(5):
            login_with_csrf(client, TEST_USERNAME, "wrong")
            # Pretend each attempt came from a different "forwarded" address.
            client.headers.update({"X-Forwarded-For": f"10.10.10.{i}"})
        r = login_with_csrf(client, TEST_USERNAME, TEST_PASSWORD)
    assert r.status_code == 429


# ---------- #3: 24h overlap on incremental sync ----------


def test_incremental_after_lowers_cursor_for_recent_rows(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    c = connect(db)
    try:
        apply_pending(c)
        # Three rows: row_id 5 from yesterday, 10 + 11 from earlier today.
        # Without overlap the cursor would be 11; we want it lowered to 9
        # so re-fetching the in-window rows is allowed.
        c.execute(
            "INSERT INTO watch_events (source_row_id, started_at) "
            "VALUES (5, datetime('now', '-2 day'))"
        )
        c.execute(
            "INSERT INTO watch_events (source_row_id, started_at) "
            "VALUES (10, datetime('now', '-1 hour'))"
        )
        c.execute(
            "INSERT INTO watch_events (source_row_id, started_at) "
            "VALUES (11, datetime('now', '-30 minute'))"
        )
        c.commit()

        max_id = _max_row_id(c)
        floor = _incremental_after(c)
    finally:
        c.close()

    assert max_id == 11
    # The 24h overlap drops the cursor below the smallest in-window row (10).
    assert floor == 9


def test_incremental_after_falls_back_to_max_when_no_recent_rows(
    tmp_path: Path,
) -> None:
    db = tmp_path / "db.sqlite"
    c = connect(db)
    try:
        apply_pending(c)
        c.execute(
            "INSERT INTO watch_events (source_row_id, started_at) "
            "VALUES (1, datetime('now', '-30 day'))"
        )
        c.commit()
        floor = _incremental_after(c)
    finally:
        c.close()
    assert floor == 1


# ---------- #4: Multi-part media files ----------


def test_multipart_media_files_persist_separately(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    c = connect(db)
    try:
        apply_pending(c)
        c.execute(
            "INSERT INTO plex_items (id, rating_key, kind, title, "
            "last_seen_sync_run_id) VALUES (1, 4242, 'movie', 'Two-part Movie', 1)"
        )
        c.commit()
        # Two parts of a split file. Both should land as separate rows.
        for idx, size in enumerate((5_000_000_000, 6_000_000_000)):
            _upsert_plex_media_file(
                c,
                plex_item_id=1,
                rating_key=4242,
                fmeta=PlexFileMeta(
                    file_path=f"/movies/x/part{idx}.mkv",
                    size_bytes=size,
                    container="mkv",
                    video_resolution="1080",
                    video_codec="hevc",
                ),
                part_index=idx,
                run_id=1,
            )

        rows = c.execute(
            "SELECT part_index, file_path, size_bytes "
            "FROM plex_media_files WHERE plex_item_id = 1 ORDER BY part_index"
        ).fetchall()
    finally:
        c.close()

    assert len(rows) == 2
    assert rows[0]["part_index"] == 0
    assert rows[1]["part_index"] == 1
    assert rows[0]["file_path"].endswith("part0.mkv")
    assert rows[1]["file_path"].endswith("part1.mkv")


def test_multipart_re_upsert_updates_existing_row(tmp_path: Path) -> None:
    """Re-running the sync must update each part row, not duplicate."""
    db = tmp_path / "db.sqlite"
    c = connect(db)
    try:
        apply_pending(c)
        c.execute(
            "INSERT INTO plex_items (id, rating_key, kind, title, "
            "last_seen_sync_run_id) VALUES (1, 4242, 'movie', 'Two-part', 1)"
        )
        c.commit()
        meta = PlexFileMeta(
            file_path="/movies/x/part0.mkv",
            size_bytes=1_000_000,
            container="mkv",
            video_resolution="720",
            video_codec="h264",
        )
        _upsert_plex_media_file(c, 1, 4242, meta, part_index=0, run_id=1)
        # Same identity, larger size on next sync.
        meta2 = PlexFileMeta(
            file_path=meta.file_path,
            size_bytes=9_000_000,
            container=meta.container,
            video_resolution=meta.video_resolution,
            video_codec=meta.video_codec,
        )
        _upsert_plex_media_file(c, 1, 4242, meta2, part_index=0, run_id=2)

        rows = c.execute(
            "SELECT COUNT(*) AS n, MAX(size_bytes) AS sz "
            "FROM plex_media_files WHERE plex_item_id = 1"
        ).fetchone()
    finally:
        c.close()
    assert rows["n"] == 1
    assert rows["sz"] == 9_000_000
