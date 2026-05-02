"""views.candidates query helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dms.db import connect
from dms.migrations import apply_pending
from dms.views import candidates as candidates_view
from dms.views import summary


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "db.sqlite")
    apply_pending(c)
    # Build a tiny fixture: 1 instance, 2 arr_items, candidates from 2 runs.
    c.execute(
        "INSERT INTO instances (id, kind, slug, name, url, api_key) "
        "VALUES (1, 'radarr', 'radarr-1', 'R', 'http://x', 'k')"
    )
    added_old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    c.execute(
        "INSERT INTO arr_items (id, instance_id, kind, arr_id, title, year, "
        "added_at, last_seen_sync_run_id) "
        "VALUES (1, 1, 'movie', 1, 'Movie A', 2020, ?, 1)",
        (added_old,),
    )
    c.execute(
        "INSERT INTO arr_items (id, instance_id, kind, arr_id, title, year, "
        "added_at, last_seen_sync_run_id) "
        "VALUES (2, 1, 'movie', 2, 'Movie B', 2021, ?, 1)",
        (added_old,),
    )
    c.execute(
        "INSERT INTO arr_files (instance_id, arr_item_id, kind, arr_file_id, "
        "size_bytes) VALUES (1, 1, 'movie', 100, 1000000000)"
    )
    c.execute(
        "INSERT INTO arr_files (instance_id, arr_item_id, kind, arr_file_id, "
        "size_bytes) VALUES (1, 2, 'movie', 200, 5000000000)"
    )
    # Two sync_jobs, the second is the latest.
    c.execute(
        "INSERT INTO sync_jobs (id, kind, status, started_at) "
        "VALUES (1, 'manual', 'succeeded', datetime('now'))"
    )
    c.execute(
        "INSERT INTO sync_jobs (id, kind, status, started_at) "
        "VALUES (2, 'manual', 'partial', datetime('now'))"
    )
    # Candidates: same arr_item_id=1 across BOTH runs (only run 2 should surface).
    c.execute(
        "INSERT INTO candidates (arr_item_id, reason, scope, size_bytes, "
        "age_days, confidence, computed_at_sync_run_id) "
        "VALUES (1, 'never_watched_anyone', 'anyone', 1000000000, 200, 'high', 1)"
    )
    c.execute(
        "INSERT INTO candidates (arr_item_id, reason, scope, size_bytes, "
        "age_days, confidence, computed_at_sync_run_id) "
        "VALUES (1, 'never_watched_anyone', 'anyone', 1000000000, 200, 'high', 2)"
    )
    c.execute(
        "INSERT INTO candidates (arr_item_id, reason, scope, size_bytes, "
        "age_days, confidence, computed_at_sync_run_id) "
        "VALUES (2, 'stale_finished_anyone', 'anyone', 5000000000, 365, 'high', 2)"
    )
    c.commit()
    yield c
    c.close()


def test_latest_run_picks_partial_or_succeeded(conn) -> None:
    run = candidates_view.latest_run(conn)
    assert run is not None
    assert run.id == 2
    assert run.status == "partial"


def test_list_candidates_filters_by_run_and_reason(conn) -> None:
    rows, total = candidates_view.list_candidates(
        conn,
        run_id=2,
        reasons=("never_watched_anyone",),
    )
    assert total == 1
    assert rows[0].title == "Movie A"


def test_list_candidates_filters_by_instance(conn) -> None:
    rows, total = candidates_view.list_candidates(
        conn,
        run_id=2,
        reasons=("never_watched_anyone", "stale_finished_anyone"),
        instance_slug="radarr-1",
    )
    assert total == 2


def test_list_candidates_unknown_instance_filters_to_empty(conn) -> None:
    rows, total = candidates_view.list_candidates(
        conn,
        run_id=2,
        reasons=("never_watched_anyone",),
        instance_slug="nope",
    )
    assert total == 0
    assert rows == []


def test_list_candidates_paginates(conn) -> None:
    rows, total = candidates_view.list_candidates(
        conn,
        run_id=2,
        reasons=("never_watched_anyone", "stale_finished_anyone"),
        per_page=1,
        page=1,
    )
    assert total == 2
    assert len(rows) == 1


def test_list_candidates_orders_by_size_desc(conn) -> None:
    rows, total = candidates_view.list_candidates(
        conn,
        run_id=2,
        reasons=("never_watched_anyone", "stale_finished_anyone"),
        sort="size",
    )
    assert [r.title for r in rows] == ["Movie B", "Movie A"]


def test_reasons_for_tab() -> None:
    assert candidates_view.reasons_for_tab("never", scope="anyone") == ["never_watched_anyone"]
    assert candidates_view.reasons_for_tab("never", scope="requester") == [
        "never_watched_requester"
    ]
    assert "stale_partial_anyone" in candidates_view.reasons_for_tab("stale", scope="anyone")
    assert "stale_finished_anyone" in candidates_view.reasons_for_tab(
        "stale", scope="anyone", state="finished"
    )
    assert candidates_view.reasons_for_tab("orphans") == [
        "orphan_arr_no_plex",
        "orphan_plex_no_arr",
    ]


def test_summary_reason_summary_counts(conn) -> None:
    rows = summary.reason_summary(conn, run_id=2)
    by_reason = {r.reason: r for r in rows}
    assert by_reason["never_watched_anyone"].count == 1
    assert by_reason["stale_finished_anyone"].count == 1
    assert by_reason["never_watched_anyone"].total_bytes == 1_000_000_000


def test_summary_headline_dedupes_by_arr_item(conn) -> None:
    # Run 2: arr_item 1 once, arr_item 2 once → 2 distinct items, 6_000_000_000.
    items, total = summary.headline_reclaim_bytes(conn, run_id=2)
    assert items == 2
    assert total == 6_000_000_000


def test_summary_age_buckets(conn) -> None:
    buckets = summary.age_buckets_for_never_watched(conn, run_id=2)
    # arr_item 1 is 200 days old → falls in 90-365 bucket.
    by_label = {b.label: b for b in buckets}
    assert by_label["90–365 days"].count == 1
    assert by_label["0–30 days"].count == 0
