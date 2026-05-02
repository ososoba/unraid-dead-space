"""Coverage for the Codex review pass: secret scrubbing, file-level orphans,
DB file mode, manual user-mapping, Tautulli iter_history retention cap."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from dms.app import create_app
from dms.clients.base import UpstreamHTTPError, scrub_secrets
from dms.db import DB_FILE_MODE, connect
from dms.migrations import apply_pending
from dms.sync.candidates_db import compute_candidates
from dms.sync.tautulli_sync import _retention_cutoff_unix
from tests.conftest import login_with_csrf

TEST_PASSWORD = "hunter2-correct-horse"
TEST_USERNAME = "admin"


# ---------- P1 #1: secret scrubbing in upstream errors ----------


class TestSecretScrubbing:
    def test_scrub_apikey_query_param(self) -> None:
        url = "http://t.local/api/v2?cmd=get_users&apikey=SECRET-123&start=0"
        clean = scrub_secrets(url)
        assert "SECRET-123" not in clean
        assert "<redacted>" in clean
        assert "cmd=get_users" in clean  # non-secret params preserved

    def test_scrub_token_param(self) -> None:
        clean = scrub_secrets("https://x/api?token=abc&y=1")
        assert "abc" not in clean

    def test_scrub_handles_uppercase_and_aliases(self) -> None:
        for raw in ("API_KEY=xyz", "ApiKey=xyz", "password=xyz", "secret=xyz"):
            assert "xyz" not in scrub_secrets(f"http://x/?{raw}")

    def test_upstream_http_error_redacts_url_and_body(self) -> None:
        exc = UpstreamHTTPError(
            500,
            "http://t/api?apikey=SECRET-123",
            'oops {"apikey": "SECRET-123"}',
        )
        msg = str(exc)
        assert "SECRET-123" not in msg
        assert "<redacted>" in msg
        assert exc.url == "http://t/api?apikey=<redacted>"


# ---------- P2 #5: DB file is 0600 ----------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_db_file_mode_is_0600(tmp_path: Path) -> None:
    db = tmp_path / "secret.sqlite"
    conn = connect(db)
    try:
        conn.execute("CREATE TABLE x (a INT)")
        conn.commit()
    finally:
        conn.close()
    assert stat.S_IMODE(db.stat().st_mode) == DB_FILE_MODE


# ---------- P2 #6: Tautulli retention cap helper ----------


def test_retention_cutoff_unix_is_in_the_past() -> None:
    cutoff = _retention_cutoff_unix(10)
    now = int(datetime.now(UTC).timestamp())
    # Should be roughly 10 years before now (allow 1 day slack).
    assert now - cutoff > (10 * 365 - 1) * 86400


# ---------- P1 #2: file-level orphan detection ----------


def _seed_orphan_fixture(db: Path) -> None:
    c = connect(db)
    apply_pending(c)
    # Two Radarr instances. Movie X exists in both with different sizes.
    # Plex only has the 4K (20 GB) version. Expect Radarr 1080p version to
    # be flagged orphan even though Plex matches by tmdb_id.
    c.executescript(
        """
        INSERT INTO instances (id, kind, slug, name, url, api_key)
        VALUES (1, 'radarr', 'radarr-1080', 'Radarr 1080p', 'http://x', 'k'),
               (2, 'radarr', 'radarr-4k',   'Radarr 4K',    'http://x', 'k');
        INSERT INTO arr_items (id, instance_id, kind, arr_id, title, tmdb_id,
                               last_seen_sync_run_id)
        VALUES
          (1, 1, 'movie', 100, 'Movie X', 555, 1),
          (2, 2, 'movie', 100, 'Movie X', 555, 1);
        INSERT INTO arr_files (instance_id, arr_item_id, kind, arr_file_id,
                               size_bytes, last_seen_sync_run_id)
        VALUES
          -- 1080p: 2 GB
          (1, 1, 'movie', 1, 2147483648, 1),
          -- 4K: 20 GB
          (2, 2, 'movie', 2, 21474836480, 1);
        INSERT INTO plex_items (id, rating_key, kind, title, tmdb_id,
                                last_seen_sync_run_id)
        VALUES (1, 9999, 'movie', 'Movie X', 555, 1);
        -- Plex has only the 4K file (20 GB).
        INSERT INTO plex_media_files (plex_item_id, rating_key, size_bytes,
                                      last_seen_sync_run_id)
        VALUES (1, 9999, 21474836480, 1);
        INSERT INTO sync_jobs (id, kind, status, started_at)
        VALUES (1, 'manual', 'succeeded', datetime('now'));
        """
    )
    c.close()


def test_orphan_arr_no_plex_flags_unmatched_quality(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    _seed_orphan_fixture(db)
    c = connect(db)
    try:
        compute_candidates(c, run_id=1)
        rows = c.execute(
            """SELECT ai.title, i.slug, c.size_bytes
               FROM candidates c
               JOIN arr_items ai ON ai.id = c.arr_item_id
               JOIN instances i ON i.id = ai.instance_id
               WHERE c.reason = 'orphan_arr_no_plex'
               ORDER BY c.size_bytes"""
        ).fetchall()
    finally:
        c.close()
    slugs = [r["slug"] for r in rows]
    # Only the 1080p Radarr version should be orphan; the 4K matches Plex.
    assert slugs == ["radarr-1080"]


def test_orphan_arr_no_plex_quiet_when_size_matches(tmp_path: Path) -> None:
    """If Plex has BOTH qualities, neither Arr instance is orphaned."""
    db = tmp_path / "db.sqlite"
    _seed_orphan_fixture(db)
    c = connect(db)
    try:
        # Add a 1080p Plex media file too.
        c.execute(
            """INSERT INTO plex_items (rating_key, kind, title, tmdb_id,
                                       last_seen_sync_run_id)
               VALUES (10000, 'movie', 'Movie X', 555, 1)"""
        )
        plex_id = c.execute("SELECT id FROM plex_items WHERE rating_key = 10000").fetchone()[0]
        c.execute(
            """INSERT INTO plex_media_files (plex_item_id, rating_key, size_bytes,
                                             last_seen_sync_run_id)
               VALUES (?, 10000, 2147483648, 1)""",
            (plex_id,),
        )
        c.commit()

        compute_candidates(c, run_id=1)
        rows = c.execute(
            "SELECT COUNT(*) FROM candidates WHERE reason = 'orphan_arr_no_plex'"
        ).fetchone()
    finally:
        c.close()
    assert rows[0] == 0


# ---------- P2 #8: manual user-mapping endpoint ----------


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
    )
    monkeypatch.setenv("SESSION_SECRET", "test-secret-key-must-be-at-least-16-chars")
    monkeypatch.setenv("COOKIE_SECURE", "false")


@pytest.fixture
def authed_client(auth_env: None, tmp_path: Path) -> Iterator[TestClient]:
    db = tmp_path / "db.sqlite"
    c = connect(db)
    apply_pending(c)
    # Seed an unresolved mapping + a Tautulli user that watch_events knows about.
    c.executescript(
        """
        INSERT INTO user_identity_map (requester_source, requester_id, requester_name,
            tautulli_user_id, tautulli_user_name, match_method, confidence)
        VALUES ('overseerr', 42, 'Alex', NULL, NULL, 'unresolved', 'low');
        INSERT INTO watch_events (source_row_id, rating_key, kind, user_id, user_name,
            started_at, percent_complete)
        VALUES (1, 100, 'movie', 7, 'alex_p', datetime('now'), 95);
        """
    )
    c.close()
    app = create_app(db_path=db, enable_scheduler=False)
    with TestClient(app) as client:
        login_with_csrf(client, TEST_USERNAME, TEST_PASSWORD)
        yield client


def test_user_mapping_save_records_manual(authed_client: TestClient) -> None:
    page = authed_client.get("/config")
    csrf = _csrf_from_page(page.text)
    r = authed_client.post(
        "/config/user-mapping",
        headers={"X-CSRF-Token": csrf},
        data={"requester_source": "overseerr", "requester_id": "42", "tautulli_user_id": "7"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["tautulli_user_id"] == 7
    assert body["tautulli_user_name"] == "alex_p"

    # Reflect in DB as match_method='manual', confidence='high'.
    db_path = authed_client.app.state.db_path
    c = connect(db_path)
    try:
        row = c.execute(
            "SELECT tautulli_user_id, match_method, confidence "
            "FROM user_identity_map WHERE requester_source='overseerr' AND requester_id=42"
        ).fetchone()
    finally:
        c.close()
    assert row["tautulli_user_id"] == 7
    assert row["match_method"] == "manual"
    assert row["confidence"] == "high"


def test_user_mapping_clear_unmaps(authed_client: TestClient) -> None:
    page = authed_client.get("/config")
    csrf = _csrf_from_page(page.text)
    # First map.
    authed_client.post(
        "/config/user-mapping",
        headers={"X-CSRF-Token": csrf},
        data={"requester_source": "overseerr", "requester_id": "42", "tautulli_user_id": "7"},
    )
    # Then clear by sending empty tautulli_user_id.
    r = authed_client.post(
        "/config/user-mapping",
        headers={"X-CSRF-Token": csrf},
        data={"requester_source": "overseerr", "requester_id": "42", "tautulli_user_id": ""},
    )
    assert r.status_code == 200
    db_path = authed_client.app.state.db_path
    c = connect(db_path)
    try:
        row = c.execute(
            "SELECT tautulli_user_id, match_method "
            "FROM user_identity_map WHERE requester_source='overseerr' AND requester_id=42"
        ).fetchone()
    finally:
        c.close()
    assert row["tautulli_user_id"] is None
    assert row["match_method"] == "manual"


def test_user_mapping_requires_csrf(authed_client: TestClient) -> None:
    r = authed_client.post(
        "/config/user-mapping",
        data={"requester_source": "overseerr", "requester_id": "42", "tautulli_user_id": "7"},
    )
    assert r.status_code == 403


def _csrf_from_page(html: str) -> str:
    marker = 'name="_csrf" value="'
    idx = html.find(marker)
    assert idx >= 0
    start = idx + len(marker)
    return html[start : html.find('"', start)]


# Avoid noqa-style warnings about unused `os` etc.
_ = os
