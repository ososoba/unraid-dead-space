"""Dashboard snapshot capture, delta computation, sparkline rendering,
and the trend strip on the homepage.

Covers:
- `take_snapshot` writes one row per reason + the TOTAL headline row.
- Re-running a sync (same run_id) does not duplicate rows.
- Pruning drops snapshots older than `retention_days`.
- `latest_with_delta` computes bytes/count/pct vs the previous snapshot.
- `series` returns oldest-first chart-friendly data.
- `sparkline` renders a deterministic SVG with the correct number of
  points; small / empty inputs degrade gracefully.
- `signed_humansize` / `signed_pct` render explicit +/− prefixes.
- Home page renders the trend strip with delta + sparkline once two
  snapshots exist.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from dms.app import create_app
from dms.db import connect
from dms.formatters import signed_humansize, signed_pct, sparkline
from dms.migrations import apply_pending
from dms.sync.snapshots import TOTAL_KEY, take_snapshot
from dms.views.snapshots import latest_with_delta, latest_with_delta_many, series
from tests.conftest import login_with_csrf

TEST_PASSWORD = "hunter2-correct-horse"
TEST_USERNAME = "admin"


# ---------- Helpers ----------


def _seed_run(conn, *, run_id: int, candidates: list[tuple[int, str, int]]) -> None:
    """Seed a sync_jobs row + candidates rows for a given run."""
    conn.execute(
        "INSERT INTO sync_jobs (id, kind, status, started_at) "
        "VALUES (?, 'manual', 'succeeded', datetime('now'))",
        (run_id,),
    )
    # Need an instance + arr_item to satisfy candidate FK shape (arr_item_id
    # is nullable but the canonical case references one).
    conn.execute(
        "INSERT OR IGNORE INTO instances (id, kind, slug, name, url, api_key) "
        "VALUES (1, 'radarr', 'r1', 'R', 'http://x', 'k')",
    )
    for arr_item_id, reason, size in candidates:
        conn.execute(
            "INSERT OR IGNORE INTO arr_items (id, instance_id, kind, arr_id, "
            "title, year, added_at, last_seen_sync_run_id) "
            "VALUES (?, 1, 'movie', ?, 'M', 2020, datetime('now'), ?)",
            (arr_item_id, arr_item_id, run_id),
        )
        conn.execute(
            "INSERT INTO candidates (arr_item_id, reason, scope, size_bytes, "
            "age_days, confidence, computed_at_sync_run_id) "
            "VALUES (?, ?, 'anyone', ?, 100, 'high', ?)",
            (arr_item_id, reason, size, run_id),
        )
    conn.commit()


# ---------- take_snapshot + read helpers ----------


class TestTakeSnapshot:
    def test_writes_total_plus_per_reason_rows(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        c = connect(db)
        apply_pending(c)
        _seed_run(
            c,
            run_id=1,
            candidates=[
                (1, "never_watched_anyone", 1_000_000_000),
                (2, "never_watched_anyone", 2_000_000_000),
                (3, "stale_finished_anyone", 5_000_000_000),
            ],
        )
        n = take_snapshot(c, run_id=1)
        # 1 TOTAL + 2 distinct reasons
        assert n == 3
        rows = {
            r["reason"]: (r["item_count"], r["total_bytes"])
            for r in c.execute("SELECT reason, item_count, total_bytes FROM dashboard_snapshots")
        }
        assert rows[TOTAL_KEY] == (3, 8_000_000_000)
        assert rows["never_watched_anyone"] == (2, 3_000_000_000)
        assert rows["stale_finished_anyone"] == (1, 5_000_000_000)
        c.close()

    def test_total_dedupes_arr_item_across_reasons(self, tmp_path: Path) -> None:
        """Same arr_item flagged by 2 reasons should count ONCE in TOTAL,
        with MAX(size) — not summed twice."""
        db = tmp_path / "db.sqlite"
        c = connect(db)
        apply_pending(c)
        _seed_run(
            c,
            run_id=1,
            candidates=[
                (1, "never_watched_anyone", 1_000_000_000),
                (1, "stale_finished_anyone", 1_000_000_000),
            ],
        )
        take_snapshot(c, run_id=1)
        total = c.execute(
            "SELECT item_count, total_bytes FROM dashboard_snapshots WHERE reason = ?",
            (TOTAL_KEY,),
        ).fetchone()
        # DISTINCT arr_item → 1 item, MAX(size) → 1 GB (not 2 GB)
        assert total["item_count"] == 1
        assert total["total_bytes"] == 1_000_000_000
        c.close()

    def test_idempotent_per_run_id(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        c = connect(db)
        apply_pending(c)
        _seed_run(c, run_id=1, candidates=[(1, "never_watched_anyone", 1_000_000_000)])
        first = take_snapshot(c, run_id=1)
        second = take_snapshot(c, run_id=1)
        assert first == 2  # TOTAL + 1 reason
        assert second == 0  # no-op
        c.close()

    def test_prunes_old_snapshots(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        c = connect(db)
        apply_pending(c)
        # Pre-insert an ancient row tied to a prior sync run.
        c.execute(
            "INSERT INTO sync_jobs (id, kind, status, started_at) "
            "VALUES (99, 'manual', 'succeeded', datetime('now', '-400 days'))",
        )
        c.execute(
            "INSERT INTO dashboard_snapshots (sync_run_id, reason, item_count, "
            "total_bytes, taken_at) "
            "VALUES (99, 'ancient', 0, 0, datetime('now', '-400 days'))",
        )
        c.commit()
        # New run triggers take_snapshot, which both inserts AND prunes.
        _seed_run(c, run_id=1, candidates=[(1, "never_watched_anyone", 1_000_000_000)])
        take_snapshot(c, run_id=1, retention_days=30)
        kept = {r["reason"] for r in c.execute("SELECT reason FROM dashboard_snapshots")}
        assert "ancient" not in kept
        assert TOTAL_KEY in kept
        c.close()


class TestReadHelpers:
    def test_latest_with_delta_computes_diffs(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        c = connect(db)
        apply_pending(c)
        _seed_run(
            c,
            run_id=1,
            candidates=[(1, "never_watched_anyone", 10_000_000_000)],
        )
        take_snapshot(c, run_id=1)
        _seed_run(
            c,
            run_id=2,
            candidates=[
                (1, "never_watched_anyone", 10_000_000_000),
                (2, "never_watched_anyone", 5_000_000_000),
            ],
        )
        take_snapshot(c, run_id=2)

        stat = latest_with_delta(c, "never_watched_anyone")
        assert stat is not None
        assert stat.latest.item_count == 2
        assert stat.latest.total_bytes == 15_000_000_000
        assert stat.previous is not None
        assert stat.previous.total_bytes == 10_000_000_000
        assert stat.delta is not None
        assert stat.delta.bytes_delta == 5_000_000_000
        assert stat.delta.count_delta == 1
        assert stat.delta.pct == pytest.approx(50.0)
        c.close()

    def test_latest_with_delta_no_previous(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        c = connect(db)
        apply_pending(c)
        _seed_run(c, run_id=1, candidates=[(1, "never_watched_anyone", 1_000_000_000)])
        take_snapshot(c, run_id=1)
        stat = latest_with_delta(c, "never_watched_anyone")
        assert stat is not None
        assert stat.previous is None
        assert stat.delta is None
        c.close()

    def test_latest_with_delta_many_skips_missing(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        c = connect(db)
        apply_pending(c)
        _seed_run(c, run_id=1, candidates=[(1, "never_watched_anyone", 1_000_000_000)])
        take_snapshot(c, run_id=1)
        out = latest_with_delta_many(c, ["never_watched_anyone", "stale_finished_anyone"])
        assert "never_watched_anyone" in out
        assert "stale_finished_anyone" not in out
        c.close()

    def test_pct_none_when_previous_zero(self, tmp_path: Path) -> None:
        """If the prior snapshot was 0 bytes (e.g. a fresh install), pct
        is None to avoid divide-by-zero — UI renders '—'."""
        db = tmp_path / "db.sqlite"
        c = connect(db)
        apply_pending(c)
        # Run 1: empty
        c.execute(
            "INSERT INTO sync_jobs (id, kind, status, started_at) "
            "VALUES (1, 'manual', 'succeeded', datetime('now'))",
        )
        c.execute(
            "INSERT INTO dashboard_snapshots (sync_run_id, reason, item_count, "
            "total_bytes) VALUES (1, ?, 0, 0)",
            (TOTAL_KEY,),
        )
        # Run 2: has data
        c.execute(
            "INSERT INTO sync_jobs (id, kind, status, started_at) "
            "VALUES (2, 'manual', 'succeeded', datetime('now'))",
        )
        c.execute(
            "INSERT INTO dashboard_snapshots (sync_run_id, reason, item_count, "
            "total_bytes) VALUES (2, ?, 1, 1000)",
            (TOTAL_KEY,),
        )
        c.commit()
        stat = latest_with_delta(c, TOTAL_KEY)
        assert stat is not None and stat.delta is not None
        assert stat.delta.bytes_delta == 1000
        assert stat.delta.pct is None
        c.close()

    def test_series_returns_oldest_first(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        c = connect(db)
        apply_pending(c)
        for run_id, size in enumerate([1_000, 2_000, 3_000], start=1):
            _seed_run(
                c,
                run_id=run_id,
                candidates=[(run_id, "never_watched_anyone", size)],
            )
            take_snapshot(c, run_id=run_id)
        pts = series(c, TOTAL_KEY, limit=10)
        assert [p.total_bytes for p in pts] == [1000, 2000, 3000]
        c.close()


# ---------- Formatters ----------


@dataclass(frozen=True)
class _P:
    total_bytes: int


class TestSparkline:
    def test_empty_returns_empty(self) -> None:
        assert sparkline([]) == ""

    def test_single_point_returns_empty(self) -> None:
        assert sparkline([_P(100)]) == ""

    def test_renders_polyline_with_n_points(self) -> None:
        svg = sparkline([_P(100), _P(200), _P(150), _P(300)])
        assert svg.startswith("<svg")
        assert "polyline" in svg
        # 4 input points → 4 coordinate pairs in `points` attribute
        assert svg.count(",") >= 4

    def test_uses_currentcolor_for_theming(self) -> None:
        svg = sparkline([_P(1), _P(2)])
        assert 'stroke="currentColor"' in svg

    def test_constant_series_does_not_crash(self) -> None:
        # vrange would be 0; the helper clamps to 1 to avoid div-by-zero.
        svg = sparkline([_P(50), _P(50), _P(50)])
        assert svg.startswith("<svg")


class TestSignedFormatters:
    def test_signed_humansize_positive(self) -> None:
        assert signed_humansize(1024) == "+1.0 KB"

    def test_signed_humansize_negative(self) -> None:
        assert signed_humansize(-1024) == "−1.0 KB"

    def test_signed_humansize_zero(self) -> None:
        assert signed_humansize(0) == "0 B"

    def test_signed_humansize_none(self) -> None:
        assert signed_humansize(None) == "—"

    def test_signed_pct(self) -> None:
        assert signed_pct(12.3) == "+12.3%"
        assert signed_pct(-7.5) == "−7.5%"
        assert signed_pct(0) == "0%"
        assert signed_pct(None) == "—"


# ---------- Home page integration ----------


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(
        "APP_PASSWORD_HASH",
        bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
    )
    monkeypatch.setenv("SESSION_SECRET", "test-secret-key-must-be-at-least-16-chars")
    monkeypatch.setenv("COOKIE_SECURE", "false")


def _seed_two_runs(db: Path) -> None:
    c = connect(db)
    apply_pending(c)
    added = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    c.execute(
        "INSERT INTO instances (id, kind, slug, name, url, api_key) "
        "VALUES (1, 'radarr', 'r1', 'R', 'http://x', 'k')",
    )
    c.execute(
        "INSERT INTO arr_items (id, instance_id, kind, arr_id, title, year, "
        "added_at, last_seen_sync_run_id) VALUES "
        "(1, 1, 'movie', 1, 'A', 2020, ?, 2), "
        "(2, 1, 'movie', 2, 'B', 2021, ?, 2)",
        (added, added),
    )
    c.execute(
        "INSERT INTO arr_files (instance_id, arr_item_id, kind, arr_file_id, size_bytes) "
        "VALUES (1, 1, 'movie', 100, 1000000000), (1, 2, 'movie', 200, 5000000000)",
    )
    c.commit()
    # First run: only Movie A is a candidate.
    c.execute(
        "INSERT INTO sync_jobs (id, kind, status, started_at) "
        "VALUES (1, 'manual', 'succeeded', datetime('now', '-1 day'))",
    )
    c.execute(
        "INSERT INTO candidates (arr_item_id, reason, scope, size_bytes, age_days, "
        "confidence, computed_at_sync_run_id) VALUES "
        "(1, 'never_watched_anyone', 'anyone', 1000000000, 200, 'high', 1)",
    )
    c.commit()
    take_snapshot(c, run_id=1)
    # Second run (latest): both items are candidates → reclaim grew.
    c.execute(
        "INSERT INTO sync_jobs (id, kind, status, started_at) "
        "VALUES (2, 'manual', 'succeeded', datetime('now'))",
    )
    c.execute(
        "INSERT INTO candidates (arr_item_id, reason, scope, size_bytes, age_days, "
        "confidence, computed_at_sync_run_id) VALUES "
        "(1, 'never_watched_anyone', 'anyone', 1000000000, 200, 'high', 2), "
        "(2, 'never_watched_anyone', 'anyone', 5000000000, 365, 'high', 2)",
    )
    c.commit()
    take_snapshot(c, run_id=2)
    c.close()


@pytest.fixture
def client(auth_env: None, tmp_path: Path) -> Iterator[TestClient]:
    db = tmp_path / "db.sqlite"
    _seed_two_runs(db)
    app = create_app(db_path=db, enable_scheduler=False)
    with TestClient(app) as c:
        login_with_csrf(c, TEST_USERNAME, TEST_PASSWORD)
        yield c


class TestHomeTrendStrip:
    def test_renders_sparkline_when_two_snapshots_exist(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200
        # SVG sparkline is inlined inside the headline card.
        assert 'class="sparkline"' in r.text
        assert "polyline" in r.text

    def test_renders_headline_delta_with_sign(self, client: TestClient) -> None:
        r = client.get("/")
        # Reclaim grew by 5_000_000_000 bytes between run 1 and run 2.
        # 5 GB = 4.7 GiB under our 1024-base humansize → "+4.7 GB".
        assert "+4.7 GB" in r.text
        # Trend pill carries the up/down semantic class.
        assert 'class="trend up"' in r.text

    def test_first_run_has_no_trend(self, auth_env: None, tmp_path: Path) -> None:
        """A brand-new install with one snapshot must NOT crash and must
        not render a delta pill."""
        db = tmp_path / "db.sqlite"
        c = connect(db)
        apply_pending(c)
        c.execute(
            "INSERT INTO instances (id, kind, slug, name, url, api_key) "
            "VALUES (1, 'radarr', 'r1', 'R', 'http://x', 'k')",
        )
        c.execute(
            "INSERT INTO arr_items (id, instance_id, kind, arr_id, title, year, "
            "added_at, last_seen_sync_run_id) "
            "VALUES (1, 1, 'movie', 1, 'A', 2020, datetime('now'), 1)",
        )
        c.execute(
            "INSERT INTO sync_jobs (id, kind, status, started_at) "
            "VALUES (1, 'manual', 'succeeded', datetime('now'))",
        )
        c.execute(
            "INSERT INTO candidates (arr_item_id, reason, scope, size_bytes, "
            "age_days, confidence, computed_at_sync_run_id) "
            "VALUES (1, 'never_watched_anyone', 'anyone', 1000, 100, 'high', 1)",
        )
        c.commit()
        take_snapshot(c, run_id=1)
        c.close()
        app = create_app(db_path=db, enable_scheduler=False)
        with TestClient(app) as cli:
            login_with_csrf(cli, TEST_USERNAME, TEST_PASSWORD)
            r = cli.get("/")
        assert r.status_code == 200
        # No previous snapshot → no trend pill, no sparkline (need >=2 points).
        assert 'class="trend up"' not in r.text
        assert 'class="trend down"' not in r.text
        assert 'class="sparkline"' not in r.text
