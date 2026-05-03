"""Candidate query helpers for the dashboards.

All views read from the latest succeeded/partial sync run only; older
candidate rows are kept for debugging but never surfaced.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

NEVER_REASONS = ("never_watched_anyone", "never_watched_requester")
STALE_REASONS = (
    "stale_finished_anyone",
    "stale_finished_requester",
    "stale_partial_anyone",
    "stale_partial_requester",
)
ORPHAN_REASONS = ("orphan_arr_no_plex", "orphan_plex_no_arr")
ALL_REASONS = NEVER_REASONS + STALE_REASONS + ORPHAN_REASONS

SortKey = Literal["size", "added", "last_played", "coverage"]
StaleFilter = Literal["all", "finished", "partial"]


@dataclass(frozen=True)
class LatestRun:
    id: int
    kind: str
    status: str
    started_at: str
    finished_at: str | None


def latest_run(conn: sqlite3.Connection) -> LatestRun | None:
    row = conn.execute(
        """
        SELECT id, kind, status, started_at, finished_at
        FROM sync_jobs
        WHERE status IN ('succeeded', 'partial')
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return LatestRun(**{k: row[k] for k in ("id", "kind", "status", "started_at", "finished_at")})


@dataclass
class CandidateRow:
    candidate_id: int
    arr_item_id: int | None
    plex_item_id: int | None
    title: str
    year: int | None
    kind: str | None
    instance_slug: str | None
    instance_name: str | None
    reason: str
    scope: str
    size_bytes: int
    age_days: int | None
    last_played_at: str | None
    confidence: str
    requester_name: str | None
    requester_source: str | None
    coverage_anyone: float | None
    coverage_requester: float | None
    last_played_at_anyone: str | None
    last_played_at_requester: str | None
    is_finished_anyone: int | None
    is_finished_requester: int | None
    plex_file_size: int | None
    plex_file_path: str | None


def _select_columns() -> str:
    return """
        c.id AS candidate_id,
        c.arr_item_id, c.plex_item_id, c.reason, c.scope, c.size_bytes,
        c.age_days, c.last_played_at, c.confidence,
        COALESCE(ai.title, pi.title, '(unknown)') AS title,
        COALESCE(ai.year, pi.year) AS year,
        ai.kind AS kind,
        i.slug AS instance_slug, i.name AS instance_name,
        ra.requester_name, ra.source AS requester_source,
        ws.episode_coverage_pct_anyone AS coverage_anyone,
        ws.episode_coverage_pct_requester AS coverage_requester,
        ws.last_played_at_anyone, ws.last_played_at_requester,
        ws.is_finished_anyone, ws.is_finished_requester,
        pmf.size_bytes AS plex_file_size,
        pmf.file_path AS plex_file_path
    """


def _from_join() -> str:
    return """
        FROM candidates c
        LEFT JOIN arr_items ai ON ai.id = c.arr_item_id
        LEFT JOIN instances i ON i.id = ai.instance_id
        LEFT JOIN plex_items pi ON pi.id = c.plex_item_id
        LEFT JOIN plex_media_files pmf ON pmf.id = c.plex_media_file_id
        LEFT JOIN request_attribution ra ON ra.arr_item_id = c.arr_item_id
        LEFT JOIN watch_state ws ON ws.arr_item_id = c.arr_item_id
    """


def _order_by(sort: SortKey) -> str:
    return {
        "size": "c.size_bytes DESC",
        "added": "ai.added_at ASC",
        "last_played": "c.last_played_at ASC NULLS FIRST",
        "coverage": "ws.episode_coverage_pct_anyone ASC NULLS FIRST",
    }[sort]


def _row_to_candidate(row: sqlite3.Row) -> CandidateRow:
    return CandidateRow(**{f: row[f] for f in CandidateRow.__dataclass_fields__})


def list_candidates(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    reasons: Sequence[str] | None = None,
    instance_slug: str | None = None,
    requester_name: str | None = None,
    age_min_days: int | None = None,
    age_max_days: int | None = None,
    title_query: str | None = None,
    sort: SortKey = "size",
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[CandidateRow], int]:
    """Return (rows, total_count) for the given filter.

    All filters are optional. Pass `reasons=None` (or omit) for "any reason".
    `requester_name` matches request_attribution.requester_name exactly;
    `age_min_days` / `age_max_days` filter on candidates.age_days inclusively;
    `title_query` does a case-insensitive substring search on arr_items.title
    (also falls back to plex_items.title for plex-only orphans).
    """
    where: list[str] = ["c.computed_at_sync_run_id = ?"]
    params: list[object] = [run_id]
    if reasons:
        placeholders = ",".join("?" for _ in reasons)
        where.append(f"c.reason IN ({placeholders})")
        params.extend(reasons)
    if instance_slug:
        where.append("i.slug = ?")
        params.append(instance_slug)
    if requester_name:
        where.append("ra.requester_name = ?")
        params.append(requester_name)
    if age_min_days is not None:
        where.append("c.age_days IS NOT NULL AND c.age_days >= ?")
        params.append(age_min_days)
    if age_max_days is not None:
        where.append("c.age_days IS NOT NULL AND c.age_days < ?")
        params.append(age_max_days)
    if title_query:
        where.append(
            "(LOWER(COALESCE(ai.title, '')) LIKE ?  OR LOWER(COALESCE(pi.title, '')) LIKE ?)"
        )
        like = f"%{title_query.lower()}%"
        params.extend([like, like])

    where_clause = " AND ".join(where)

    count_sql = "SELECT COUNT(*) " + _from_join() + " WHERE " + where_clause
    total = int(conn.execute(count_sql, params).fetchone()[0])

    offset = max(0, (page - 1) * per_page)
    list_sql = (
        "SELECT "
        + _select_columns()
        + _from_join()
        + " WHERE "
        + where_clause
        + f" ORDER BY {_order_by(sort)} LIMIT ? OFFSET ?"
    )
    rows = conn.execute(list_sql, [*params, per_page, offset]).fetchall()
    return [_row_to_candidate(r) for r in rows], total


def reasons_for_tab(tab: str, *, scope: str = "anyone", state: StaleFilter = "all") -> list[str]:
    """Map UI tab + scope + stale filter to candidate reasons."""
    suffix = "_anyone" if scope == "anyone" else "_requester"
    if tab == "never":
        return [f"never_watched{suffix}"]
    if tab == "stale":
        bases = []
        if state in ("all", "finished"):
            bases.append(f"stale_finished{suffix}")
        if state in ("all", "partial"):
            bases.append(f"stale_partial{suffix}")
        return bases
    if tab == "orphans":
        return list(ORPHAN_REASONS)
    raise ValueError(f"unknown tab {tab!r}")


def reason_label(reason: str) -> str:
    return {
        "never_watched_anyone": "Never watched (anyone)",
        "never_watched_requester": "Never watched by requester",
        "stale_finished_anyone": "Finished, stale",
        "stale_finished_requester": "Finished by requester, stale",
        "stale_partial_anyone": "Partially watched, stale",
        "stale_partial_requester": "Partially watched by requester, stale",
        "orphan_arr_no_plex": "In Sonarr/Radarr but not in Plex",
        "orphan_plex_no_arr": "In Plex but not in Sonarr/Radarr",
    }.get(reason, reason)
