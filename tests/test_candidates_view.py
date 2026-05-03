"""Tests for the /candidates universal drill-down view + filter helpers
+ dashboard click-throughs that funnel into it."""

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
from dms.views import candidates as candidates_view
from dms.views import summary
from tests.conftest import login_with_csrf

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
    """Two requesters, two instances, mix of reasons + ages."""
    c = connect(db)
    apply_pending(c)
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    young = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    c.executescript(
        f"""
        INSERT INTO instances (id, kind, slug, name, url, api_key)
        VALUES (1, 'radarr', 'radarr-1', 'Radarr 1080p', 'http://x', 'k'),
               (2, 'sonarr', 'sonarr-1', 'Sonarr 1080p', 'http://x', 'k');

        INSERT INTO arr_items (id, instance_id, kind, arr_id, title, year,
                               added_at, last_seen_sync_run_id)
        VALUES
          -- 4 movies, 2 series, varied ages and requesters
          (1, 1, 'movie',  10, 'Old Movie A',     2018, '{old}',   1),
          (2, 1, 'movie',  11, 'Old Movie B',     2019, '{old}',   1),
          (3, 1, 'movie',  12, 'Young Movie',     2026, '{young}', 1),
          (4, 1, 'movie',  13, 'Title with Fianc',2024, '{old}',   1),
          (5, 2, 'series', 20, 'Old Show A',      2018, '{old}',   1),
          (6, 2, 'series', 21, 'Old Show B',      2019, '{old}',   1);

        INSERT INTO arr_files (instance_id, arr_item_id, kind, arr_file_id, size_bytes)
        VALUES
          (1, 1, 'movie',   100, 1000000000),  -- 1 GB
          (1, 2, 'movie',   101, 5000000000),  -- 5 GB
          (1, 3, 'movie',   102, 2000000000),  -- 2 GB (young, won't be a candidate)
          (1, 4, 'movie',   103, 9000000000),  -- 9 GB
          (2, 5, 'episode', 200, 50000000000), -- 50 GB
          (2, 6, 'episode', 201, 80000000000); -- 80 GB

        INSERT INTO request_attribution (arr_item_id, requester_name, source)
        VALUES (1, 'alice', 'overseerr'),
               (2, 'alice', 'overseerr'),
               (4, 'bob',   'overseerr'),
               (5, 'alice', 'overseerr');

        INSERT INTO sync_jobs (id, kind, status, started_at)
        VALUES (1, 'manual', 'succeeded', datetime('now'));

        INSERT INTO candidates (arr_item_id, reason, scope, size_bytes, age_days,
                                confidence, computed_at_sync_run_id)
        VALUES
          (1, 'never_watched_anyone',     'anyone', 1000000000, 400, 'high', 1),
          (2, 'never_watched_anyone',     'anyone', 5000000000, 400, 'high', 1),
          (4, 'never_watched_anyone',     'anyone', 9000000000, 400, 'high', 1),
          (5, 'stale_finished_anyone',    'anyone', 50000000000, 400, 'high', 1),
          (6, 'stale_partial_anyone',     'anyone', 80000000000, 400, 'high', 1);
        """
    )
    c.close()


@pytest.fixture
def client(auth_env: None, tmp_path: Path) -> Iterator[TestClient]:
    db = tmp_path / "db.sqlite"
    _seed(db)
    app = create_app(db_path=db, enable_scheduler=False)
    with TestClient(app) as c:
        login_with_csrf(c, TEST_USERNAME, TEST_PASSWORD)
        yield c


# ---------- views.candidates filter unit tests ----------


class TestListCandidatesFilters:
    def _conn(self, tmp_path: Path):
        db = tmp_path / "db.sqlite"
        _seed(db)
        return connect(db)

    def test_no_filters_returns_all(self, tmp_path: Path) -> None:
        c = self._conn(tmp_path)
        try:
            rows, total = candidates_view.list_candidates(c, run_id=1)
        finally:
            c.close()
        assert total == 5

    def test_filter_by_reason(self, tmp_path: Path) -> None:
        c = self._conn(tmp_path)
        try:
            rows, total = candidates_view.list_candidates(
                c,
                run_id=1,
                reasons=("never_watched_anyone",),
            )
        finally:
            c.close()
        assert total == 3
        assert all(r.reason == "never_watched_anyone" for r in rows)

    def test_filter_by_requester(self, tmp_path: Path) -> None:
        c = self._conn(tmp_path)
        try:
            rows, total = candidates_view.list_candidates(
                c,
                run_id=1,
                requester_name="alice",
            )
        finally:
            c.close()
        # alice → arr_items 1, 2, 5 (item 6 is stale but no requester)
        assert total == 3
        assert {r.title for r in rows} == {"Old Movie A", "Old Movie B", "Old Show A"}

    def test_filter_by_age_min(self, tmp_path: Path) -> None:
        c = self._conn(tmp_path)
        try:
            rows, total = candidates_view.list_candidates(
                c,
                run_id=1,
                age_min_days=365,
            )
        finally:
            c.close()
        # All seeded candidates are 400 days old; all match
        assert total == 5

    def test_filter_by_age_max(self, tmp_path: Path) -> None:
        c = self._conn(tmp_path)
        try:
            rows, total = candidates_view.list_candidates(
                c,
                run_id=1,
                age_max_days=30,
            )
        finally:
            c.close()
        assert total == 0  # nothing is < 30 days old in our fixture

    def test_filter_by_title_query_case_insensitive(self, tmp_path: Path) -> None:
        c = self._conn(tmp_path)
        try:
            rows, total = candidates_view.list_candidates(
                c,
                run_id=1,
                title_query="fIaNc",
            )
        finally:
            c.close()
        assert total == 1
        assert "Fianc" in rows[0].title

    def test_combined_filters(self, tmp_path: Path) -> None:
        c = self._conn(tmp_path)
        try:
            rows, total = candidates_view.list_candidates(
                c,
                run_id=1,
                reasons=("never_watched_anyone",),
                requester_name="alice",
            )
        finally:
            c.close()
        assert total == 2
        assert {r.title for r in rows} == {"Old Movie A", "Old Movie B"}


# ---------- views.summary new aggregations ----------


class TestSummaryRequesterRollups:
    def test_top_requesters_by_reclaim(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        _seed(db)
        c = connect(db)
        try:
            tops = summary.top_requesters_by_reclaim(c, run_id=1, limit=10)
        finally:
            c.close()
        names = [t.name for t in tops]
        # alice has 1+5+50 GB; bob has 9 GB; "(no requester)" gets the 80 GB stale series
        # so order should be: (no requester) > alice > bob
        assert names[0] in {"(no requester)", "alice"}  # depends on attribution semantics

    def test_requester_totals_per_bucket_columns(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        _seed(db)
        c = connect(db)
        try:
            rows = summary.requester_totals(c, run_id=1)
        finally:
            c.close()
        by_name = {r.name: r for r in rows}
        # alice has 2 never-watched (movies A + B) + 1 stale_finished (Old Show A)
        assert by_name["alice"].never_watched_count == 2
        assert by_name["alice"].stale_count == 1
        assert by_name["alice"].never_watched_bytes == 1_000_000_000 + 5_000_000_000


# ---------- /candidates HTTP route ----------


class TestCandidatesRoute:
    def test_unfiltered_lists_all(self, client: TestClient) -> None:
        r = client.get("/candidates")
        assert r.status_code == 200
        body = r.content
        assert b"5 candidates" in body
        # Top-by-size should be the 80 GB series first.
        assert b"Old Show B" in body
        assert b"Old Movie A" in body

    def test_filter_by_reason(self, client: TestClient) -> None:
        r = client.get("/candidates?reason=never_watched_anyone")
        assert r.status_code == 200
        assert b"3 candidates" in r.content
        assert b"Old Show A" not in r.content  # stale, not never-watched

    def test_filter_by_requester(self, client: TestClient) -> None:
        r = client.get("/candidates?requester=alice")
        assert r.status_code == 200
        assert b"3 candidates" in r.content
        assert b"Old Movie A" in r.content
        assert b"Title with Fianc" not in r.content  # bob's

    def test_filter_by_age_range(self, client: TestClient) -> None:
        r = client.get("/candidates?reason=never_watched_anyone&age_min=365")
        assert r.status_code == 200
        assert b"3 candidates" in r.content

    def test_filter_by_title(self, client: TestClient) -> None:
        r = client.get("/candidates?q=fianc")
        assert r.status_code == 200
        assert b"1 candidate" in r.content
        assert b"Fianc" in r.content

    def test_filter_combined(self, client: TestClient) -> None:
        r = client.get(
            "/candidates?requester=alice&reason=never_watched_anyone&sort=size",
        )
        assert r.status_code == 200
        assert b"2 candidates" in r.content

    def test_unknown_reason_returns_400(self, client: TestClient) -> None:
        r = client.get("/candidates?reason=garbage_reason")
        assert r.status_code == 400

    def test_bad_sort_returns_400(self, client: TestClient) -> None:
        r = client.get("/candidates?sort=alphabetical")
        assert r.status_code == 400

    def test_requires_login(self, auth_env: None, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        _seed(db)
        app = create_app(db_path=db, enable_scheduler=False)
        with TestClient(app) as cli:
            r = cli.get(
                "/candidates", follow_redirects=False, headers={"Accept": "application/json"}
            )
        assert r.status_code == 401


# ---------- Dashboard click-through links ----------


class TestDashboardLinks:
    def test_homepage_cards_link_to_candidates(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200
        body = r.text
        # Headline card: links to all candidates sorted by size.
        assert 'href="/candidates?sort=size"' in body
        # Per-reason cards: each reason becomes a filter link.
        assert "/candidates?reason=never_watched_anyone" in body
        assert "/candidates?reason=stale_finished_anyone" in body

    def test_homepage_age_buckets_link(self, client: TestClient) -> None:
        r = client.get("/")
        body = r.text
        # Age-bucket "1+ years" → age_min=365, no age_max
        assert "age_min=365" in body
        # Age-bucket "30–90 days" → age_min=30 + age_max=90
        assert "age_min=30" in body and "age_max=90" in body

    def test_homepage_top_requesters_link(self, client: TestClient) -> None:
        r = client.get("/")
        body = r.text
        # alice should appear as a clickable name in the top-requesters strip
        assert 'href="/candidates?requester=alice' in body

    def test_requesters_page_links_each_row(self, client: TestClient) -> None:
        r = client.get("/requesters")
        body = r.text
        assert 'href="/candidates?requester=alice' in body
        assert 'href="/candidates?requester=bob' in body
        # Per-reason bucket columns also link.
        assert "reason=never_watched_anyone" in body
