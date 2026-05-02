"""Candidate engine logic tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dms.candidates import (
    ArrItemView,
    CandidateConfig,
    WatchSummary,
    compute_candidates,
)

NOW = datetime(2026, 5, 1, tzinfo=UTC)
CFG = CandidateConfig(never_watched_days=90, stale_days=180)


def _movie(
    *,
    title: str = "Test Movie",
    added_days_ago: int = 365,
    size: int = 1_000_000_000,
    watch: WatchSummary | None = None,
    requester: str | None = None,
    requester_resolved: bool = True,
    has_plex: bool = True,
) -> ArrItemView:
    return ArrItemView(
        instance_slug="radarr-1",
        instance_name="Radarr 1080p",
        arr_id=1,
        kind="movie",
        title=title,
        year=2020,
        tmdb_id=123,
        tvdb_id=None,
        imdb_id=None,
        size_bytes=size,
        added_at=NOW - timedelta(days=added_days_ago),
        requester_name=requester,
        requester_resolved=requester_resolved,
        has_plex_match=has_plex,
        watch=watch or WatchSummary(),
    )


class TestNeverWatched:
    def test_emits_when_old_and_unwatched(self) -> None:
        cands = compute_candidates([_movie(added_days_ago=200)], config=CFG, now=NOW)
        reasons = {c.reason for c in cands}
        assert "never_watched_anyone" in reasons

    def test_skips_when_too_new(self) -> None:
        cands = compute_candidates([_movie(added_days_ago=30)], config=CFG, now=NOW)
        reasons = {c.reason for c in cands}
        assert "never_watched_anyone" not in reasons

    def test_skips_when_watched(self) -> None:
        watch = WatchSummary(has_any_play=True, last_played_at_anyone=NOW - timedelta(days=10))
        cands = compute_candidates([_movie(added_days_ago=200, watch=watch)], config=CFG, now=NOW)
        reasons = {c.reason for c in cands}
        assert "never_watched_anyone" not in reasons


class TestStale:
    def test_stale_finished_movie(self) -> None:
        watch = WatchSummary(
            has_any_play=True,
            is_finished_anyone=True,
            last_played_at_anyone=NOW - timedelta(days=200),
        )
        cands = compute_candidates([_movie(watch=watch)], config=CFG, now=NOW)
        assert any(c.reason == "stale_finished_anyone" for c in cands)

    def test_not_stale_when_recent(self) -> None:
        watch = WatchSummary(
            has_any_play=True,
            is_finished_anyone=True,
            last_played_at_anyone=NOW - timedelta(days=30),
        )
        cands = compute_candidates([_movie(watch=watch)], config=CFG, now=NOW)
        assert not any(c.reason.startswith("stale_") for c in cands)

    def test_stale_partial_when_not_finished(self) -> None:
        watch = WatchSummary(
            has_any_play=True,
            is_finished_anyone=False,
            last_played_at_anyone=NOW - timedelta(days=200),
        )
        cands = compute_candidates([_movie(watch=watch)], config=CFG, now=NOW)
        assert any(c.reason == "stale_partial_anyone" for c in cands)


class TestRequesterScope:
    def test_skipped_without_requester(self) -> None:
        cands = compute_candidates([_movie(added_days_ago=200)], config=CFG, now=NOW)
        assert not any(c.scope == "requester" for c in cands)

    def test_low_confidence_when_unresolved(self) -> None:
        cands = compute_candidates(
            [_movie(added_days_ago=200, requester="moyin", requester_resolved=False)],
            config=CFG,
            now=NOW,
        )
        req_cands = [c for c in cands if c.scope == "requester"]
        assert req_cands
        assert all(c.confidence == "low" for c in req_cands)

    def test_high_confidence_when_resolved(self) -> None:
        cands = compute_candidates(
            [_movie(added_days_ago=200, requester="moyin", requester_resolved=True)],
            config=CFG,
            now=NOW,
        )
        req_cands = [c for c in cands if c.scope == "requester"]
        assert req_cands
        assert all(c.confidence == "high" for c in req_cands)

    def test_requester_separately_from_anyone(self) -> None:
        # Watched by someone (not requester) — anyone-scope skipped, requester-scope flagged
        watch = WatchSummary(
            has_any_play=True,
            has_requester_play=False,
            last_played_at_anyone=NOW - timedelta(days=10),
        )
        cands = compute_candidates(
            [_movie(added_days_ago=200, watch=watch, requester="moyin")],
            config=CFG,
            now=NOW,
        )
        reasons = {c.reason for c in cands}
        assert "never_watched_anyone" not in reasons
        assert "never_watched_requester" in reasons


class TestOrphan:
    def test_emits_orphan_when_no_plex(self) -> None:
        cands = compute_candidates([_movie(has_plex=False)], config=CFG, now=NOW)
        assert any(c.reason == "orphan_arr_no_plex" for c in cands)
