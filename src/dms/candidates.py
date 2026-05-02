"""Candidate engine — in-memory bucket computation for the spike.

Inputs are already-pulled-and-resolved objects. Output is a list of
`Candidate` records ready to JSON-serialize.

This is a lean spike implementation:
- Movies only: full implementation with anyone/requester scope.
- Series: aggregate finished-anyone (episode coverage) only; per-requester
  coverage stubbed as TODO once `arr_episodes` ↔ `plex_items` ↔ `watch_events`
  joins are validated against real data. Recorded in the candidate record so
  PLAN.md decision #6 is honored when we move past the spike.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

CandidateReason = Literal[
    "never_watched_anyone",
    "never_watched_requester",
    "stale_finished_anyone",
    "stale_finished_requester",
    "stale_partial_anyone",
    "stale_partial_requester",
    "orphan_arr_no_plex",
    "orphan_plex_no_arr",
]

Confidence = Literal["high", "medium", "low"]


@dataclass
class WatchSummary:
    """Per-arr-item watch state, pre-computed by the spike."""

    has_any_play: bool = False
    has_requester_play: bool = False
    last_played_at_anyone: datetime | None = None
    last_played_at_requester: datetime | None = None
    is_finished_anyone: bool = False
    is_finished_requester: bool = False
    episode_coverage_pct_anyone: float | None = None
    episode_coverage_pct_requester: float | None = None


@dataclass
class ArrItemView:
    """Lightweight projection of an arr_item for the candidate engine."""

    instance_slug: str
    instance_name: str
    arr_id: int
    kind: Literal["movie", "series"]
    title: str
    year: int | None
    tmdb_id: int | None
    tvdb_id: int | None
    imdb_id: str | None
    size_bytes: int
    added_at: datetime | None
    requester_name: str | None
    requester_resolved: bool  # whether requester ↔ Tautulli mapping is known
    has_plex_match: bool  # for orphan detection
    watch: WatchSummary = field(default_factory=WatchSummary)


@dataclass
class Candidate:
    arr_item_id: str | None  # f"{instance_slug}:{arr_id}", null for plex orphans
    instance_slug: str | None
    title: str
    year: int | None
    kind: str | None
    reason: CandidateReason
    scope: Literal["anyone", "requester"]
    size_bytes: int
    age_days: int | None
    last_played_at: str | None
    requester_name: str | None
    confidence: Confidence


@dataclass
class CandidateConfig:
    never_watched_days: int = 90
    stale_days: int = 180


def _age_days(now: datetime, when: datetime | None) -> int | None:
    if when is None:
        return None
    return (now - when).days


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _confidence_for_requester_scope(item: ArrItemView) -> Confidence:
    return "high" if item.requester_resolved else "low"


def _emit_anyone(item: ArrItemView, cfg: CandidateConfig, now: datetime) -> list[Candidate]:
    out: list[Candidate] = []
    age = _age_days(now, item.added_at)

    if not item.watch.has_any_play and age is not None and age > cfg.never_watched_days:
        out.append(_make(item, "never_watched_anyone", "anyone", age, None, "high"))

    if item.watch.has_any_play and item.watch.last_played_at_anyone:
        days_since = _age_days(now, item.watch.last_played_at_anyone) or 0
        if days_since > cfg.stale_days:
            reason: CandidateReason = (
                "stale_finished_anyone" if item.watch.is_finished_anyone else "stale_partial_anyone"
            )
            out.append(
                _make(
                    item,
                    reason,
                    "anyone",
                    age,
                    item.watch.last_played_at_anyone,
                    "high",
                )
            )
    return out


def _emit_requester(item: ArrItemView, cfg: CandidateConfig, now: datetime) -> list[Candidate]:
    """Requester-scope candidates. Skip if no requester known at all."""
    if not item.requester_name:
        return []
    out: list[Candidate] = []
    age = _age_days(now, item.added_at)
    conf = _confidence_for_requester_scope(item)

    if not item.watch.has_requester_play and age is not None and age > cfg.never_watched_days:
        out.append(_make(item, "never_watched_requester", "requester", age, None, conf))

    if item.watch.has_requester_play and item.watch.last_played_at_requester:
        days_since = _age_days(now, item.watch.last_played_at_requester) or 0
        if days_since > cfg.stale_days:
            reason: CandidateReason = (
                "stale_finished_requester"
                if item.watch.is_finished_requester
                else "stale_partial_requester"
            )
            out.append(
                _make(
                    item,
                    reason,
                    "requester",
                    age,
                    item.watch.last_played_at_requester,
                    conf,
                )
            )
    return out


def _make(
    item: ArrItemView,
    reason: CandidateReason,
    scope: Literal["anyone", "requester"],
    age: int | None,
    last_played: datetime | None,
    confidence: Confidence,
) -> Candidate:
    return Candidate(
        arr_item_id=f"{item.instance_slug}:{item.arr_id}",
        instance_slug=item.instance_slug,
        title=item.title,
        year=item.year,
        kind=item.kind,
        reason=reason,
        scope=scope,
        size_bytes=item.size_bytes,
        age_days=age,
        last_played_at=_iso(last_played),
        requester_name=item.requester_name,
        confidence=confidence,
    )


def compute_candidates(
    items: Iterable[ArrItemView],
    *,
    config: CandidateConfig,
    now: datetime | None = None,
) -> list[Candidate]:
    """Compute all candidate rows for the given items (excludes orphans)."""
    now = now or datetime.now(UTC)
    out: list[Candidate] = []
    for item in items:
        out.extend(_emit_anyone(item, config, now))
        out.extend(_emit_requester(item, config, now))
        if not item.has_plex_match:
            out.append(_make(item, "orphan_arr_no_plex", "anyone", None, None, "high"))
    return out


def candidates_to_dicts(candidates: list[Candidate]) -> list[dict]:
    return [asdict(c) for c in candidates]
