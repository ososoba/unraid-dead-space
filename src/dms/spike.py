"""CLI spike entry point.

Pulls Sonarr/Radarr/Tautulli/(Overseerr|Seerr) read-only, builds identity +
user maps, computes candidates, prints JSON to stdout. No DB writes.

Usage:
    python -m dms.spike [--limit N] [--reason <reason>] [--pretty]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from dms.candidates import (
    ArrItemView,
    CandidateConfig,
    WatchSummary,
    candidates_to_dicts,
    compute_candidates,
)
from dms.clients.arr import ArrClient
from dms.clients.base import UpstreamError
from dms.clients.requester import RequesterClient
from dms.clients.tautulli import TautulliClient
from dms.config import AppConfig, ArrInstance, RequesterConfig, load_config
from dms.identity import (
    UserMapping,
    map_requesters_to_tautulli,
    resolve_plex_item,
)
from dms.models import (
    ArrMovie,
    ArrSeries,
    RequestRecord,
    TautulliHistoryRow,
    TautulliLibraryItem,
    TautulliUser,
)
from dms.tag_parser import ParsedTag

log = logging.getLogger("dms.spike")


@dataclass
class InstanceReport:
    slug: str
    name: str
    kind: str
    ok: bool
    error: str | None = None
    item_count: int = 0
    total_size_bytes: int = 0


@dataclass
class RequesterReport:
    slug: str
    name: str
    source: str
    ok: bool
    error: str | None = None
    request_count: int = 0
    user_count: int = 0


@dataclass
class SpikeReport:
    started_at: str
    finished_at: str | None = None
    instances: list[InstanceReport] = field(default_factory=list)
    tautulli_ok: bool = False
    tautulli_history_rows: int = 0
    tautulli_library_items: int = 0
    requesters: list[RequesterReport] = field(default_factory=list)
    user_mapping_resolved: int = 0
    user_mapping_unresolved: int = 0
    identity_resolved: int = 0
    identity_unresolved: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    candidate_summary: dict[str, int] = field(default_factory=dict)


# ---------- Pull helpers ----------


async def _pull_arr(
    instance: ArrInstance, timeout: float
) -> tuple[InstanceReport, list[ArrMovie] | list[ArrSeries], dict[int, str]]:
    """Return (report, items, tag_label_by_id)."""
    report = InstanceReport(slug=instance.slug, name=instance.name, kind=instance.kind, ok=False)
    items: list[ArrMovie] | list[ArrSeries] = []
    tags_by_id: dict[int, str] = {}
    try:
        async with ArrClient(instance, timeout=timeout) as client:
            tag_rows = await client.list_tags()
            tags_by_id = {t.id: t.label for t in tag_rows}
            if instance.kind == "radarr":
                items = await client.list_movies()
                report.total_size_bytes = sum(i.size_on_disk for i in items)
            else:
                items = await client.list_series()
                report.total_size_bytes = sum(i.size_on_disk for i in items)
            report.item_count = len(items)
            report.ok = True
    except UpstreamError as exc:
        report.error = str(exc)
        log.warning("arr pull failed for %s: %s", instance.slug, exc)
    return report, items, tags_by_id


async def _pull_tautulli(
    config: AppConfig,
) -> tuple[bool, list[TautulliHistoryRow], list[TautulliLibraryItem], list[TautulliUser]]:
    if not config.tautulli:
        return False, [], [], []
    try:
        async with TautulliClient(config.tautulli, timeout=config.http.timeout_seconds) as t:
            users = await t.list_users()
            libraries = await t.list_libraries()
            library_items: list[TautulliLibraryItem] = []
            for lib in libraries:
                section_id = lib.get("section_id")
                if section_id is None:
                    continue
                library_items.extend(await t.library_media_info(int(section_id)))
            history = await t.history(length=config.http.backfill_page_size)
        return True, history, library_items, users
    except UpstreamError as exc:
        log.warning("tautulli pull failed: %s", exc)
        return False, [], [], []


async def _pull_requester_one(
    instance: RequesterConfig, timeout: float
) -> tuple[RequesterReport, list[RequestRecord], list]:
    """Pull users + requests from a single requester instance."""
    report = RequesterReport(
        slug=instance.slug, name=instance.name, source=instance.source, ok=False
    )
    try:
        async with RequesterClient(instance, timeout=timeout) as r:
            users = await r.list_users()
            requests = await r.list_requests()
        report.ok = True
        report.user_count = len(users)
        report.request_count = len(requests)
        return report, requests, users
    except UpstreamError as exc:
        report.error = str(exc)
        log.warning("requester pull failed for %s: %s", instance.slug, exc)
        return report, [], []


# ---------- Resolution + projection ----------


def _build_plex_index(
    library: list[TautulliLibraryItem],
) -> dict[str, TautulliLibraryItem]:
    """Index plex items by 'tmdb:<id>' / 'tvdb:<id>' / 'imdb:<id>' for fast lookup."""
    index: dict[str, TautulliLibraryItem] = {}
    for item in library:
        ids = resolve_plex_item(item)
        if ids.tmdb_id:
            index.setdefault(f"tmdb:{ids.tmdb_id}", item)
        if ids.tvdb_id:
            index.setdefault(f"tvdb:{ids.tvdb_id}", item)
        if ids.imdb_id:
            index.setdefault(f"imdb:{ids.imdb_id}", item)
    return index


def _project_movie(
    instance: ArrInstance,
    movie: ArrMovie,
    tag_labels: dict[int, str],
    plex_index: dict[str, TautulliLibraryItem],
    history_by_rating_key: dict[int, list[TautulliHistoryRow]],
    user_mapping: dict[int | None, UserMapping],
    requested_by_external: dict[tuple[str, int | None, str | None], RequestRecord],
) -> ArrItemView:
    requester_name = _resolve_requester(
        movie.tags, tag_labels, "movie", movie.tmdb_id, None, requested_by_external
    )
    requester_resolved = bool(
        requester_name
        and any(
            m.confidence != "low" and m.requester_name == requester_name
            for m in user_mapping.values()
        )
    )

    plex_item = (plex_index.get(f"tmdb:{movie.tmdb_id}") if movie.tmdb_id else None) or (
        plex_index.get(f"imdb:{movie.imdb_id}") if movie.imdb_id else None
    )

    history_rows: list[TautulliHistoryRow] = []
    if plex_item is not None:
        history_rows = history_by_rating_key.get(plex_item.rating_key, [])

    watch = _summarize_watch_movie(history_rows, requester_name, user_mapping)

    return ArrItemView(
        instance_slug=instance.slug,
        instance_name=instance.name,
        arr_id=movie.id,
        kind="movie",
        title=movie.title,
        year=movie.year,
        tmdb_id=movie.tmdb_id,
        tvdb_id=None,
        imdb_id=movie.imdb_id,
        size_bytes=movie.size_on_disk,
        added_at=movie.added,
        requester_name=requester_name,
        requester_resolved=requester_resolved,
        has_plex_match=plex_item is not None,
        watch=watch,
    )


def _project_series(
    instance: ArrInstance,
    series: ArrSeries,
    tag_labels: dict[int, str],
    plex_index: dict[str, TautulliLibraryItem],
    history_by_grandparent: dict[int, list[TautulliHistoryRow]],
    user_mapping: dict[int | None, UserMapping],
    requested_by_external: dict[tuple[str, int | None, str | None], RequestRecord],
) -> ArrItemView:
    requester_name = _resolve_requester(
        series.tags, tag_labels, "tv", series.tmdb_id, series.tvdb_id, requested_by_external
    )
    requester_resolved = bool(
        requester_name
        and any(
            m.confidence != "low" and m.requester_name == requester_name
            for m in user_mapping.values()
        )
    )

    plex_item = (plex_index.get(f"tvdb:{series.tvdb_id}") if series.tvdb_id else None) or (
        plex_index.get(f"tmdb:{series.tmdb_id}") if series.tmdb_id else None
    )

    history_rows: list[TautulliHistoryRow] = []
    if plex_item is not None:
        history_rows = history_by_grandparent.get(plex_item.rating_key, [])

    watch = _summarize_watch_series(
        history_rows, series.episode_count, requester_name, user_mapping
    )

    return ArrItemView(
        instance_slug=instance.slug,
        instance_name=instance.name,
        arr_id=series.id,
        kind="series",
        title=series.title,
        year=series.year,
        tmdb_id=series.tmdb_id,
        tvdb_id=series.tvdb_id,
        imdb_id=series.imdb_id,
        size_bytes=series.size_on_disk,
        added_at=series.added,
        requester_name=requester_name,
        requester_resolved=requester_resolved,
        has_plex_match=plex_item is not None,
        watch=watch,
    )


def _resolve_requester(
    tag_ids: list[int],
    tag_labels: dict[int, str],
    media_kind: str,
    tmdb_id: int | None,
    tvdb_id: int | None,
    requested_by_external: dict[tuple[str, int | None, str | None], RequestRecord],
) -> str | None:
    """Resolution order: requests table → parsed tag → multi → me/None."""
    key = (media_kind, tmdb_id, tvdb_id)
    req = requested_by_external.get(key)
    if req and req.requested_by:
        return req.requested_by.display_name or req.requested_by.username

    parsed = [ParsedTag.parse(tag_labels[t]) for t in tag_ids if t in tag_labels]
    requester_tags = [p for p in parsed if not p.is_unparseable and p.requester_name]
    if len(requester_tags) == 1:
        return requester_tags[0].requester_name
    if len(requester_tags) > 1:
        return "multi-requester"
    return None  # "me" attribution lives at display layer


def _summarize_watch_movie(
    rows: list[TautulliHistoryRow],
    requester_name: str | None,
    user_mapping: dict[int | None, UserMapping],
) -> WatchSummary:
    if not rows:
        return WatchSummary()
    threshold_pct = 80
    plays = [r for r in rows if (r.percent_complete or 0) >= threshold_pct]
    has_any = bool(plays)
    last_anyone = max(
        (datetime.fromtimestamp(r.stopped or r.started or r.date or 0, tz=UTC) for r in plays),
        default=None,
    )
    requester_user_id = _requester_to_tautulli(requester_name, user_mapping)
    requester_plays = [p for p in plays if p.user_id == requester_user_id]
    has_requester = bool(requester_plays)
    last_requester = max(
        (
            datetime.fromtimestamp(r.stopped or r.started or r.date or 0, tz=UTC)
            for r in requester_plays
        ),
        default=None,
    )
    return WatchSummary(
        has_any_play=has_any,
        has_requester_play=has_requester,
        last_played_at_anyone=last_anyone,
        last_played_at_requester=last_requester,
        is_finished_anyone=has_any,  # movie = single-watch finished
        is_finished_requester=has_requester,
    )


def _summarize_watch_series(
    rows: list[TautulliHistoryRow],
    total_episodes: int,
    requester_name: str | None,
    user_mapping: dict[int | None, UserMapping],
) -> WatchSummary:
    if not rows:
        return WatchSummary()
    threshold_pct = 80
    plays = [r for r in rows if (r.percent_complete or 0) >= threshold_pct]
    if not plays:
        return WatchSummary()

    last_anyone = max(
        datetime.fromtimestamp(r.stopped or r.started or r.date or 0, tz=UTC) for r in plays
    )
    # episode coverage = unique (rating_key) count over total
    watched_rk = {r.rating_key for r in plays if r.rating_key}
    coverage_anyone = (len(watched_rk) / total_episodes * 100) if total_episodes else None

    requester_user_id = _requester_to_tautulli(requester_name, user_mapping)
    req_plays = [p for p in plays if p.user_id == requester_user_id]
    has_req = bool(req_plays)
    last_req = max(
        (datetime.fromtimestamp(r.stopped or r.started or r.date or 0, tz=UTC) for r in req_plays),
        default=None,
    )
    watched_rk_req = {r.rating_key for r in req_plays if r.rating_key}
    coverage_req = (len(watched_rk_req) / total_episodes * 100) if total_episodes else None

    return WatchSummary(
        has_any_play=True,
        has_requester_play=has_req,
        last_played_at_anyone=last_anyone,
        last_played_at_requester=last_req,
        is_finished_anyone=(coverage_anyone or 0) >= 100,
        is_finished_requester=(coverage_req or 0) >= 100,
        episode_coverage_pct_anyone=coverage_anyone,
        episode_coverage_pct_requester=coverage_req,
    )


def _requester_to_tautulli(
    requester_name: str | None, user_mapping: dict[int | None, UserMapping]
) -> int | None:
    if not requester_name:
        return None
    for m in user_mapping.values():
        if m.requester_name == requester_name and m.tautulli_user_id is not None:
            return m.tautulli_user_id
    return None


# ---------- Orchestrator ----------


async def run_spike(config: AppConfig, *, limit: int, reason_filter: str | None) -> SpikeReport:
    report = SpikeReport(started_at=datetime.now(UTC).isoformat())

    arr_results = await asyncio.gather(
        *(_pull_arr(inst, config.http.timeout_seconds) for inst in config.arr_instances)
    )
    report.instances = [r for r, _, _ in arr_results]

    taut_ok, history, library, taut_users = await _pull_tautulli(config)
    report.tautulli_ok = taut_ok
    report.tautulli_history_rows = len(history)
    report.tautulli_library_items = len(library)

    requester_results = await asyncio.gather(
        *(
            _pull_requester_one(inst, config.http.timeout_seconds)
            for inst in config.requester_instances
        )
    )
    report.requesters = [r for r, _, _ in requester_results]
    requests: list[RequestRecord] = []
    requester_users_seen: dict[int, Any] = {}  # dedupe across instances by user.id
    for _, instance_requests, instance_users in requester_results:
        requests.extend(instance_requests)
        for user in instance_users:
            requester_users_seen.setdefault(user.id, user)
    requester_users = list(requester_users_seen.values())

    # Identity map stats
    resolved = sum(1 for it in library if not resolve_plex_item(it).empty)
    report.identity_resolved = resolved
    report.identity_unresolved = len(library) - resolved

    # User mapping
    mappings = map_requesters_to_tautulli(requester_users, taut_users)
    report.user_mapping_resolved = sum(1 for m in mappings if m.method != "unresolved")
    report.user_mapping_unresolved = sum(1 for m in mappings if m.method == "unresolved")
    user_mapping = {m.requester_id: m for m in mappings}

    # Pre-compute lookups
    plex_index = _build_plex_index(library)
    history_by_rk: dict[int, list[TautulliHistoryRow]] = {}
    history_by_grandparent: dict[int, list[TautulliHistoryRow]] = {}
    for row in history:
        if row.media_type == "movie" and row.rating_key:
            history_by_rk.setdefault(row.rating_key, []).append(row)
        elif row.media_type == "episode" and row.grandparent_rating_key:
            history_by_grandparent.setdefault(row.grandparent_rating_key, []).append(row)

    requested_by_external: dict[tuple[str, int | None, str | None], RequestRecord] = {}
    for req in requests:
        if req.media is None:
            continue
        key = (
            req.media.media_type or "movie",
            req.media.tmdb_id,
            req.media.tvdb_id,
        )
        # earliest wins
        existing = requested_by_external.get(key)
        if existing is None or (
            req.created_at and existing.created_at and req.created_at < existing.created_at
        ):
            requested_by_external[key] = req

    # Project arr items
    views: list[ArrItemView] = []
    for instance, (_, items, tag_labels) in zip(config.arr_instances, arr_results, strict=True):
        for it in items:
            if isinstance(it, ArrMovie):
                views.append(
                    _project_movie(
                        instance,
                        it,
                        tag_labels,
                        plex_index,
                        history_by_rk,
                        user_mapping,
                        requested_by_external,
                    )
                )
            elif isinstance(it, ArrSeries):
                views.append(
                    _project_series(
                        instance,
                        it,
                        tag_labels,
                        plex_index,
                        history_by_grandparent,
                        user_mapping,
                        requested_by_external,
                    )
                )

    cands = compute_candidates(
        views,
        config=CandidateConfig(
            never_watched_days=config.watch.never_watched_days,
            stale_days=config.watch.stale_days,
        ),
    )
    if reason_filter:
        cands = [c for c in cands if c.reason == reason_filter]

    cands.sort(key=lambda c: c.size_bytes, reverse=True)

    summary: dict[str, int] = {}
    for c in cands:
        summary[c.reason] = summary.get(c.reason, 0) + 1
    report.candidate_summary = summary

    report.candidates = candidates_to_dicts(cands[:limit])
    report.finished_at = datetime.now(UTC).isoformat()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dms.spike", description="DMS read-only spike")
    parser.add_argument("--limit", type=int, default=50, help="Max candidates to print")
    parser.add_argument("--reason", type=str, default=None, help="Filter by candidate reason")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    parser.add_argument("--quiet", action="store_true", help="Suppress logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = load_config()
    if not config.arr_instances:
        log.error("No Sonarr/Radarr instances configured. Fill in .env first.")
        return 2

    report = asyncio.run(run_spike(config, limit=args.limit, reason_filter=args.reason))
    indent = 2 if args.pretty else None
    json.dump(asdict(report), sys.stdout, indent=indent, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
