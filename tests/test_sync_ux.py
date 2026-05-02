"""Sync UX: async sync trigger, status endpoints, htmx fragment, scheduler."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from dms import scheduler
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
    c = connect(db)
    apply_pending(c)
    c.executescript(
        """
        INSERT INTO sync_jobs (id, kind, status, started_at, finished_at)
        VALUES (1, 'manual', 'succeeded',
                datetime('now', '-1 hour'), datetime('now', '-55 minutes'));
        INSERT INTO sync_run_steps (run_id, step_name, status, started_at, finished_at,
            items_seen, items_changed, error_json)
        VALUES
          (1, 'arr:radarr-1', 'succeeded', datetime('now'), datetime('now'), 854, 854, NULL),
          (1, 'arr:sonarr-2', 'failed',    datetime('now'), datetime('now'),   0,   0,
           '{"error": "connection refused"}');
        """
    )
    c.close()


@pytest.fixture
def client(auth_env: None, tmp_path: Path) -> Iterator[TestClient]:
    db = tmp_path / "db.sqlite"
    _seed(db)
    # Disable scheduler for tests so cron triggers don't fire mid-test.
    app = create_app(db_path=db, enable_scheduler=False)
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


# ---------- /sync page ----------


def test_sync_page_renders(client: TestClient) -> None:
    r = client.get("/sync")
    assert r.status_code == 200
    assert b"Sync now" in r.content
    # Step rows are in the rendered fragment.
    assert b"arr:radarr-1" in r.content
    assert b"arr:sonarr-2" in r.content


def test_sync_status_json(client: TestClient) -> None:
    r = client.get("/sync/status")
    assert r.status_code == 200
    body = r.json()
    assert body["run"]["id"] == 1
    assert body["run"]["status"] == "succeeded"
    names = [s["step_name"] for s in body["steps"]]
    assert "arr:radarr-1" in names
    assert "arr:sonarr-2" in names
    assert any(s["status"] == "failed" for s in body["steps"])


def test_sync_status_fragment_html(client: TestClient) -> None:
    r = client.get("/sync/status/fragment")
    assert r.status_code == 200
    assert b"<table" in r.content
    assert b"status-failed" in r.content
    assert b"arr:radarr-1" in r.content


# ---------- /sync/run trigger ----------


def test_sync_run_requires_csrf(client: TestClient) -> None:
    r = client.post("/sync/run")
    assert r.status_code == 403


def test_sync_run_no_arr_returns_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Strip any Arr instances so load_config returns empty list.
    for kind in ("SONARR", "RADARR"):
        for i in range(1, 11):
            for suffix in ("URL", "API_KEY", "NAME"):
                monkeypatch.delenv(f"{kind}_{i}_{suffix}", raising=False)
    page = client.get("/sync")
    csrf = _csrf(page.text)
    r = client.post("/sync/run", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400


# ---------- Auth ----------


def test_sync_endpoints_require_login(auth_env: None, tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    _seed(db)
    app = create_app(db_path=db, enable_scheduler=False)
    with TestClient(app) as c:
        for path in ("/sync", "/sync/status", "/sync/status/fragment"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 401, path


# ---------- Homepage banner shows failed-step detail ----------


def test_homepage_banner_lists_failures(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    # The improved banner exposes the failed step name + error message.
    assert b"arr:sonarr-2" in r.content
    assert b"connection refused" in r.content


# ---------- Scheduler unit tests ----------


def test_scheduler_uses_default_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNC_CRON", raising=False)
    monkeypatch.setenv("TZ", "UTC")
    sched = scheduler.build_scheduler("/tmp/x.sqlite")
    job = sched.get_job(scheduler.JOB_ID)
    assert job is not None


def test_scheduler_picks_env_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_CRON", "*/15 * * * *")
    monkeypatch.setenv("TZ", "UTC")
    sched = scheduler.build_scheduler("/tmp/x.sqlite")
    job = sched.get_job(scheduler.JOB_ID)
    assert job is not None
    assert "*/15" in str(job.trigger)


def test_scheduler_invalid_cron_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_CRON", "garbage")
    with pytest.raises(ValueError):
        scheduler.build_scheduler("/tmp/x.sqlite")


# ---------- Background runner unit test ----------


def test_background_dedupes_in_process(tmp_path: Path) -> None:
    """Two rapid clicks don't start two tasks for the same DB."""
    import asyncio

    from dms.config import AppConfig
    from dms.sync import background

    # An empty config — start_background_sync still spawns a task; it'll fail
    # immediately because there's nothing to sync, which is fine — we only
    # care about the dedup gate here.
    db = str(tmp_path / "db.sqlite")
    apply_pending(connect(db))

    async def _go() -> None:
        import contextlib

        first = background.start_background_sync(
            db, AppConfig(), kind="manual", requested_by="test"
        )
        second = background.start_background_sync(
            db, AppConfig(), kind="manual", requested_by="test"
        )
        assert first is True
        assert second is False
        # Wait for the first to wind down so other tests aren't poisoned.
        existing = background._active.get(db)
        if existing is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(existing, timeout=2.0)
        background._active.pop(db, None)

    asyncio.run(_go())


# Ensure no env leakage between this and other test modules.
@pytest.fixture(autouse=True)
def _restore_env(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
