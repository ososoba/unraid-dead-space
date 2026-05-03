"""TZ-aware date formatting + scope column rendering."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from dms.app import create_app
from dms.db import connect
from dms.formatters import humandate, relative_days
from dms.migrations import apply_pending
from tests.conftest import login_with_csrf

TEST_PASSWORD = "hunter2-correct-horse"
TEST_USERNAME = "admin"


# ---------- humandate / relative_days TZ awareness ----------


class TestHumandateTzAware:
    def test_utc_iso_in_toronto_does_not_drift_a_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A play recorded at 2026-01-06 03:00 UTC is 2026-01-05 22:00 in
        Toronto. Pre-fix, we displayed the UTC date (2026-01-06). The fix
        converts to TZ first."""
        monkeypatch.setenv("TZ", "America/Toronto")
        # 2026-01-06T03:00 UTC = 2026-01-05T22:00 EST
        assert humandate("2026-01-06T03:00:00+00:00") == "2026-01-05"

    def test_utc_iso_in_utc_returns_utc_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "UTC")
        assert humandate("2026-01-06T03:00:00+00:00") == "2026-01-06"

    def test_invalid_tz_falls_back_to_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "Mars/Olympus")
        # Should not raise; should return the UTC-equivalent date.
        assert humandate("2026-01-06T03:00:00+00:00") == "2026-01-06"

    def test_naive_datetime_treated_as_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "America/Toronto")
        # Naive (no timezone) → treated as UTC → converted to Toronto.
        assert humandate("2026-01-06T03:00:00") == "2026-01-05"

    def test_humandate_empty_passes_through(self) -> None:
        assert humandate("") == "—"
        assert humandate(None) == "—"

    def test_humandate_unparseable_passes_through(self) -> None:
        assert humandate("not-a-date") == "not-a-date"

    def test_relative_days_uses_local_tz(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "America/Toronto")
        recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        # ~2 hours back, regardless of TZ, should round to "today".
        assert relative_days(recent) == "today"


# ---------- Scope column rendering ----------


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
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    c.executescript(
        f"""
        INSERT INTO instances (id, kind, slug, name, url, api_key)
        VALUES (1, 'sonarr', 'sonarr-1', 'Sonarr', 'http://x', 'k');

        INSERT INTO arr_items (id, instance_id, kind, arr_id, title, year,
                               added_at, last_seen_sync_run_id)
        VALUES (1, 1, 'series', 10, 'Both Scopes Show', 2020, '{old}', 1);

        INSERT INTO arr_files (instance_id, arr_item_id, kind, arr_file_id, size_bytes)
        VALUES (1, 1, 'episode', 100, 100000000000);

        INSERT INTO request_attribution (arr_item_id, requester_name, source)
        VALUES (1, 'moyinba', 'overseerr');

        INSERT INTO sync_jobs (id, kind, status, started_at)
        VALUES (1, 'manual', 'succeeded', datetime('now'));

        -- Same arr_item flagged in BOTH scopes
        INSERT INTO candidates (arr_item_id, reason, scope, size_bytes, age_days,
                                last_played_at, confidence, computed_at_sync_run_id)
        VALUES
          (1, 'stale_partial_anyone',    'anyone',    100000000000, 400,
           '2026-01-06T03:00:00+00:00', 'high', 1),
          (1, 'stale_partial_requester', 'requester', 100000000000, 400,
           '2024-09-11T10:00:00+00:00', 'high', 1);
        """
    )
    c.close()


@pytest.fixture
def client(auth_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("TZ", "America/Toronto")
    db = tmp_path / "db.sqlite"
    _seed(db)
    app = create_app(db_path=db, enable_scheduler=False)
    with TestClient(app) as c:
        login_with_csrf(c, TEST_USERNAME, TEST_PASSWORD)
        yield c


def test_candidates_renders_scope_column(client: TestClient) -> None:
    r = client.get("/candidates")
    assert r.status_code == 200
    html = r.text
    # Header + tagged cells for both scopes appear.
    assert '<th class="scope">Scope</th>' in html
    assert 'class="scope-tag scope-anyone"' in html
    assert 'class="scope-tag scope-requester"' in html


def test_candidates_dates_in_local_tz(client: TestClient) -> None:
    r = client.get("/candidates")
    body = r.text
    # 2026-01-06T03:00 UTC = 2026-01-05 in America/Toronto.
    # Pre-fix this was showing 2026-01-06 (UTC date) — the user-reported bug.
    assert "2026-01-05" in body
    # The requester-scope row's date stays the same since 10:00 UTC is
    # still 2024-09-11 in Toronto.
    assert "2024-09-11" in body


def test_instance_renders_scope_column(client: TestClient) -> None:
    # The seeded candidates are stale_* — view the Stale tab so a row
    # actually renders and the new column appears in the header.
    r = client.get("/instance/sonarr-1?tab=stale")
    assert r.status_code == 200
    assert '<th class="scope">Scope</th>' in r.text
    assert 'class="scope-tag scope-anyone"' in r.text
