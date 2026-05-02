"""Dashboard route tests: home / instance / requesters / ignored + ignore actions."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from dms.app import create_app
from dms.db import connect
from dms.migrations import apply_pending

TEST_PASSWORD = "hunter2-correct-horse"
TEST_USERNAME = "admin"


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
    )
    monkeypatch.setenv("SESSION_SECRET", "test-secret-key-must-be-at-least-16-chars")
    monkeypatch.setenv("COOKIE_SECURE", "false")


def _seed(db: Path) -> None:
    """Create instance + arr_items + candidates + run."""
    c = connect(db)
    apply_pending(c)
    added = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    c.executescript(
        f"""
        INSERT INTO instances (id, kind, slug, name, url, api_key)
        VALUES (1, 'radarr', 'radarr-1', 'Radarr', 'http://x', 'k');
        INSERT INTO arr_items (id, instance_id, kind, arr_id, title, year,
            added_at, last_seen_sync_run_id)
        VALUES
          (1, 1, 'movie', 1, 'Movie A', 2020, '{added}', 1),
          (2, 1, 'movie', 2, 'Movie B', 2021, '{added}', 1);
        INSERT INTO arr_files (instance_id, arr_item_id, kind, arr_file_id, size_bytes)
        VALUES (1, 1, 'movie', 100, 1000000000), (1, 2, 'movie', 200, 5000000000);
        INSERT INTO sync_jobs (id, kind, status, started_at)
        VALUES (1, 'manual', 'succeeded', datetime('now'));
        INSERT INTO candidates (arr_item_id, reason, scope, size_bytes, age_days,
            confidence, computed_at_sync_run_id)
        VALUES
          (1, 'never_watched_anyone', 'anyone', 1000000000, 200, 'high', 1),
          (2, 'stale_finished_anyone', 'anyone', 5000000000, 365, 'high', 1);
        """
    )
    c.close()


@pytest.fixture
def client(auth_env: None, tmp_path: Path) -> Iterator[TestClient]:
    db = tmp_path / "db.sqlite"
    _seed(db)
    app = create_app(db_path=db)
    with TestClient(app) as c:
        c.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
        yield c


def _csrf(text: str) -> str:
    marker = 'name="_csrf" value="'
    idx = text.find(marker)
    if idx < 0:
        return ""
    start = idx + len(marker)
    return text[start : text.find('"', start)]


# ---------- Home ----------


def test_home_renders_with_data(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert b"Dashboard" in r.content
    assert b"total reclaim potential" in r.content
    assert b"Movie A" not in r.content  # only summary on home, not row-level


def test_home_shows_age_bucket(client: TestClient) -> None:
    r = client.get("/")
    assert b"90&#8211;365 days" in r.content or b"90\xe2\x80\x93365 days" in r.content


# ---------- Instance deepdive ----------


def test_instance_deepdive_default_tab(client: TestClient) -> None:
    r = client.get("/instance/radarr-1")
    assert r.status_code == 200
    assert b"Movie A" in r.content
    # Stale tab not active.
    assert b"Movie B" not in r.content


def test_instance_deepdive_stale_tab(client: TestClient) -> None:
    r = client.get("/instance/radarr-1?tab=stale")
    assert r.status_code == 200
    assert b"Movie B" in r.content
    assert b"Movie A" not in r.content


def test_instance_deepdive_unknown_slug_404(client: TestClient) -> None:
    r = client.get("/instance/nope-1")
    assert r.status_code == 404


def test_instance_deepdive_bad_tab_400(client: TestClient) -> None:
    r = client.get("/instance/radarr-1?tab=garbage")
    assert r.status_code == 400


def test_instance_deepdive_pagination(client: TestClient) -> None:
    r = client.get("/instance/radarr-1?per_page=1")
    assert r.status_code == 200
    assert b"page 1 / 1" in r.content or b"Movie A" in r.content


# ---------- Requesters + Ignored ----------


def test_requesters_renders(client: TestClient) -> None:
    r = client.get("/requesters")
    assert r.status_code == 200
    assert b"By requester" in r.content


def test_ignored_empty(client: TestClient) -> None:
    r = client.get("/ignored")
    assert r.status_code == 200
    assert b"Nothing ignored" in r.content


# ---------- Ignore action ----------


def test_ignore_requires_csrf(client: TestClient) -> None:
    r = client.post("/items/1/ignore")
    assert r.status_code == 403


def test_ignore_then_unignore_via_csrf(client: TestClient) -> None:
    page = client.get("/instance/radarr-1")
    csrf = _csrf(page.text)
    assert csrf, "csrf token must render in instance page"

    r = client.post("/items/1/ignore", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    page2 = client.get("/ignored")
    assert b"Movie A" in page2.content

    r = client.post("/items/1/unignore", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    page3 = client.get("/ignored")
    assert b"Movie A" not in page3.content


def test_ignore_404_for_unknown_item(client: TestClient) -> None:
    page = client.get("/instance/radarr-1")
    csrf = _csrf(page.text)
    r = client.post("/items/9999/ignore", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 404


# ---------- Empty-state homepage ----------


def test_home_empty_when_no_runs(auth_env: None, tmp_path: Path) -> None:
    db = tmp_path / "empty.sqlite"
    c = connect(db)
    apply_pending(c)
    c.close()
    app = create_app(db_path=db)
    with TestClient(app) as cli:
        cli.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
        r = cli.get("/")
    assert r.status_code == 200
    assert b"No syncs yet" in r.content


# ---------- Auth required ----------


def test_dashboards_require_login(auth_env: None, tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    _seed(db)
    app = create_app(db_path=db)
    with TestClient(app) as cli:
        for path in ("/", "/instance/radarr-1", "/requesters", "/ignored"):
            r = cli.get(path, follow_redirects=False)
            assert r.status_code == 401, path
