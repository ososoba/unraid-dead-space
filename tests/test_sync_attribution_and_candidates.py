"""SQL correctness for attribution + watch_state + candidates against fixture data.

These tests build a small in-memory dataset by hand (no network) and
verify the SQL pipelines produce the right rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dms.db import connect
from dms.migrations import apply_pending
from dms.sync.attribution import recompute_request_attribution
from dms.sync.candidates_db import compute_candidates
from dms.sync.watch_state import recompute_watch_state


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "db.sqlite")
    apply_pending(c)
    # Two Arr instances: one Sonarr, one Radarr.
    c.execute(
        "INSERT INTO instances (id, kind, slug, name, url, api_key) "
        "VALUES (1, 'radarr', 'radarr-1', 'R', 'http://x', 'k')"
    )
    c.execute(
        "INSERT INTO instances (id, kind, slug, name, url, api_key) "
        "VALUES (2, 'sonarr', 'sonarr-1', 'S', 'http://x', 'k')"
    )
    c.commit()
    yield c
    c.close()


def _add_arr_movie(
    conn,
    *,
    instance_id: int,
    arr_id: int,
    tmdb_id: int | None,
    title: str,
    added_days_ago: int = 365,
    size: int = 1_000_000_000,
):
    added = (datetime.now(UTC) - timedelta(days=added_days_ago)).isoformat()
    cur = conn.execute(
        "INSERT INTO arr_items "
        "(instance_id, kind, arr_id, title, tmdb_id, added_at, last_seen_sync_run_id) "
        "VALUES (?, 'movie', ?, ?, ?, ?, 1)",
        (instance_id, arr_id, title, tmdb_id, added),
    )
    item_id = cur.lastrowid
    conn.execute(
        "INSERT INTO arr_files "
        "(instance_id, arr_item_id, kind, arr_file_id, size_bytes, last_seen_sync_run_id) "
        "VALUES (?, ?, 'movie', ?, ?, 1)",
        (instance_id, item_id, arr_id * 100, size),
    )
    conn.commit()
    return item_id


def _add_plex_movie(conn, *, rating_key: int, tmdb_id: int, title: str):
    conn.execute(
        "INSERT INTO plex_items "
        "(rating_key, kind, title, tmdb_id, last_seen_sync_run_id) "
        "VALUES (?, 'movie', ?, ?, 1)",
        (rating_key, title, tmdb_id),
    )
    conn.commit()


def _add_watch_event(
    conn,
    *,
    source_row_id: int,
    rating_key: int,
    user_id: int,
    stopped_days_ago: int = 30,
    percent: int = 95,
):
    when = (datetime.now(UTC) - timedelta(days=stopped_days_ago)).isoformat()
    conn.execute(
        "INSERT INTO watch_events "
        "(source_row_id, rating_key, kind, user_id, started_at, stopped_at, "
        " percent_complete) "
        "VALUES (?, ?, 'movie', ?, ?, ?, ?)",
        (source_row_id, rating_key, user_id, when, when, percent),
    )
    conn.commit()


def _add_request(
    conn,
    *,
    source_request_id: str,
    tmdb_id: int,
    requester_id: int,
    requester_name: str,
    source: str = "overseerr",
):
    conn.execute(
        "INSERT INTO requests "
        "(source, source_request_id, media_kind, tmdb_id, requester_id, requester_name) "
        "VALUES (?, ?, 'movie', ?, ?, ?)",
        (source, source_request_id, tmdb_id, requester_id, requester_name),
    )
    conn.commit()


def _add_user_mapping(
    conn,
    *,
    requester_source: str,
    requester_id: int,
    requester_name: str,
    tautulli_user_id: int,
    confidence: str = "high",
):
    conn.execute(
        "INSERT INTO user_identity_map "
        "(requester_source, requester_id, requester_name, "
        " tautulli_user_id, tautulli_user_name, match_method, confidence) "
        "VALUES (?, ?, ?, ?, 'u', 'api', ?)",
        (requester_source, requester_id, requester_name, tautulli_user_id, confidence),
    )
    conn.commit()


# ---------- Attribution ----------


def test_attribution_picks_request_over_tag(conn) -> None:
    _add_arr_movie(conn, instance_id=1, arr_id=1, tmdb_id=100, title="Foo")
    _add_request(
        conn,
        source_request_id="r1",
        tmdb_id=100,
        requester_id=42,
        requester_name="alex",
    )
    # Also add a tag — the request wins.
    conn.execute(
        "INSERT INTO tags (instance_id, arr_item_id, raw_tag, parsed_requester_name) "
        "VALUES (1, 1, '99 - tagger', 'tagger')"
    )
    conn.commit()

    recompute_request_attribution(conn)
    row = conn.execute("SELECT * FROM request_attribution WHERE arr_item_id = 1").fetchone()
    assert row["source"] == "overseerr"
    assert row["requester_name"] == "alex"


def test_attribution_falls_back_to_tag(conn) -> None:
    _add_arr_movie(conn, instance_id=1, arr_id=2, tmdb_id=200, title="Bar")
    conn.execute(
        "INSERT INTO tags (instance_id, arr_item_id, raw_tag, parsed_requester_id, "
        "parsed_requester_name, is_unparseable) "
        "VALUES (1, 1, '7 - moyin', 7, 'moyin', 0)"
    )
    conn.commit()

    recompute_request_attribution(conn)
    row = conn.execute("SELECT * FROM request_attribution WHERE arr_item_id = 1").fetchone()
    assert row["source"] == "tag"
    assert row["requester_name"] == "moyin"


def test_attribution_multi_requester_label(conn) -> None:
    _add_arr_movie(conn, instance_id=1, arr_id=3, tmdb_id=300, title="Baz")
    conn.executescript(
        """
        INSERT INTO tags (instance_id, arr_item_id, raw_tag, parsed_requester_id,
            parsed_requester_name, is_unparseable)
        VALUES (1, 1, '1 - one', 1, 'one', 0),
               (1, 1, '2 - two', 2, 'two', 0);
        """
    )
    conn.commit()
    recompute_request_attribution(conn)
    row = conn.execute("SELECT * FROM request_attribution WHERE arr_item_id = 1").fetchone()
    assert row["requester_name"] == "multi-requester"


# ---------- Watch state ----------


def test_movie_watch_state_anyone_only_no_requester(conn) -> None:
    _add_arr_movie(conn, instance_id=1, arr_id=1, tmdb_id=500, title="Movie")
    _add_plex_movie(conn, rating_key=5000, tmdb_id=500, title="Movie")
    _add_watch_event(conn, source_row_id=1, rating_key=5000, user_id=99, percent=95)

    recompute_watch_state(conn)
    row = conn.execute("SELECT * FROM watch_state WHERE arr_item_id = 1").fetchone()
    assert row["has_any_play"] == 1
    assert row["has_requester_play"] == 0  # no requester known
    assert row["is_finished_anyone"] == 1


def test_movie_watch_state_requester_scope(conn) -> None:
    _add_arr_movie(conn, instance_id=1, arr_id=1, tmdb_id=500, title="Movie")
    _add_plex_movie(conn, rating_key=5000, tmdb_id=500, title="Movie")
    _add_request(
        conn,
        source_request_id="r1",
        tmdb_id=500,
        requester_id=42,
        requester_name="alex",
    )
    _add_user_mapping(
        conn,
        requester_source="overseerr",
        requester_id=42,
        requester_name="alex",
        tautulli_user_id=99,
    )
    recompute_request_attribution(conn)

    # Plays by user 99 (the requester) and user 50 (someone else).
    _add_watch_event(conn, source_row_id=1, rating_key=5000, user_id=99, percent=95)
    _add_watch_event(conn, source_row_id=2, rating_key=5000, user_id=50, percent=95)

    recompute_watch_state(conn)
    row = conn.execute("SELECT * FROM watch_state WHERE arr_item_id = 1").fetchone()
    assert row["has_any_play"] == 1
    assert row["has_requester_play"] == 1
    assert row["is_finished_requester"] == 1


def test_threshold_filters_partial_plays(conn) -> None:
    _add_arr_movie(conn, instance_id=1, arr_id=1, tmdb_id=500, title="Movie")
    _add_plex_movie(conn, rating_key=5000, tmdb_id=500, title="Movie")
    _add_watch_event(conn, source_row_id=1, rating_key=5000, user_id=1, percent=20)

    recompute_watch_state(conn, threshold_movies_pct=80)
    row = conn.execute("SELECT * FROM watch_state WHERE arr_item_id = 1").fetchone()
    assert row["has_any_play"] == 0  # 20% under 80% threshold


# ---------- Candidates ----------


def test_candidates_emit_never_watched_anyone(conn) -> None:
    _add_arr_movie(
        conn,
        instance_id=1,
        arr_id=1,
        tmdb_id=500,
        title="Old",
        added_days_ago=200,
        size=10_000_000_000,
    )
    _add_plex_movie(conn, rating_key=5000, tmdb_id=500, title="Old")
    recompute_request_attribution(conn)
    recompute_watch_state(conn)

    n = compute_candidates(conn, run_id=42, never_watched_days=90, stale_days=180)
    assert n >= 1
    rows = conn.execute(
        "SELECT reason, scope, size_bytes FROM candidates "
        "WHERE computed_at_sync_run_id = 42 AND reason = 'never_watched_anyone'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["size_bytes"] == 10_000_000_000


def test_candidates_emit_orphan_arr_no_plex(conn) -> None:
    _add_arr_movie(conn, instance_id=1, arr_id=1, tmdb_id=500, title="Lost")
    # NO plex_item inserted — should be orphan.
    recompute_request_attribution(conn)
    recompute_watch_state(conn)

    compute_candidates(conn, run_id=1)
    rows = conn.execute(
        "SELECT reason FROM candidates WHERE reason = 'orphan_arr_no_plex'"
    ).fetchall()
    assert len(rows) == 1


def test_candidates_emit_orphan_plex_no_arr(conn) -> None:
    _add_plex_movie(conn, rating_key=9999, tmdb_id=777, title="Manual import")
    conn.execute(
        "INSERT INTO plex_media_files "
        "(plex_item_id, rating_key, size_bytes, last_seen_sync_run_id) "
        "VALUES (1, 9999, 5000000000, 1)"
    )
    conn.commit()
    recompute_request_attribution(conn)
    recompute_watch_state(conn)

    compute_candidates(conn, run_id=1)
    rows = conn.execute(
        "SELECT reason, arr_item_id, plex_item_id, size_bytes "
        "FROM candidates WHERE reason = 'orphan_plex_no_arr'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["arr_item_id"] is None
    assert rows[0]["plex_item_id"] is not None
    assert rows[0]["size_bytes"] == 5_000_000_000


def test_candidates_skip_ignored_items(conn) -> None:
    item_id = _add_arr_movie(
        conn,
        instance_id=1,
        arr_id=1,
        tmdb_id=500,
        title="Skip me",
        added_days_ago=200,
    )
    conn.execute("UPDATE arr_items SET ignored_local = 1 WHERE id = ?", (item_id,))
    conn.commit()
    recompute_request_attribution(conn)
    recompute_watch_state(conn)
    compute_candidates(conn, run_id=1)
    n = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE arr_item_id = ?", (item_id,)
    ).fetchone()[0]
    assert n == 0


def test_candidates_low_confidence_when_unresolved(conn) -> None:
    _add_arr_movie(conn, instance_id=1, arr_id=1, tmdb_id=500, title="X", added_days_ago=200)
    _add_plex_movie(conn, rating_key=5000, tmdb_id=500, title="X")
    _add_request(
        conn,
        source_request_id="r1",
        tmdb_id=500,
        requester_id=42,
        requester_name="ghost",
    )
    # NO user_identity_map row → mapping unresolved.
    recompute_request_attribution(conn)
    recompute_watch_state(conn)
    compute_candidates(conn, run_id=1)
    row = conn.execute(
        "SELECT confidence FROM candidates WHERE reason = 'never_watched_requester'"
    ).fetchone()
    assert row is not None
    assert row["confidence"] == "low"
