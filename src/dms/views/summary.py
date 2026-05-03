"""Aggregations powering the homepage cards."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

AGE_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0–30 days", 0, 30),
    ("30–90 days", 30, 90),
    ("90–365 days", 90, 365),
    ("1+ years", 365, None),
)


@dataclass(frozen=True)
class ReasonSummary:
    reason: str
    count: int
    total_bytes: int


@dataclass(frozen=True)
class AgeBucket:
    label: str
    count: int
    total_bytes: int


@dataclass(frozen=True)
class InstanceCard:
    slug: str
    name: str
    kind: str
    item_count: int
    total_bytes: int
    last_seen_ok_at: str | None
    last_error: str | None


def reason_summary(conn: sqlite3.Connection, run_id: int) -> list[ReasonSummary]:
    rows = conn.execute(
        """
        SELECT reason, COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS total
        FROM candidates
        WHERE computed_at_sync_run_id = ?
        GROUP BY reason
        ORDER BY total DESC
        """,
        (run_id,),
    ).fetchall()
    return [ReasonSummary(reason=r["reason"], count=r["n"], total_bytes=r["total"]) for r in rows]


def headline_reclaim_bytes(conn: sqlite3.Connection, run_id: int) -> tuple[int, int]:
    """Total reclaim potential (DISTINCT arr_item_id, max size).

    A single arr_item can show up under several reasons (e.g.
    never_watched_anyone AND never_watched_requester). Counting raw
    candidates over-counts size. We take MAX(size_bytes) per arr_item.
    """
    row = conn.execute(
        """
        SELECT
          COUNT(DISTINCT COALESCE(arr_item_id, -plex_item_id)) AS items,
          COALESCE(SUM(max_size), 0) AS total
        FROM (
          SELECT
            arr_item_id,
            plex_item_id,
            MAX(size_bytes) AS max_size
          FROM candidates
          WHERE computed_at_sync_run_id = ?
          GROUP BY arr_item_id, plex_item_id
        ) sub
        """,
        (run_id,),
    ).fetchone()
    return int(row["items"] or 0), int(row["total"] or 0)


def age_buckets_for_never_watched(conn: sqlite3.Connection, run_id: int) -> list[AgeBucket]:
    out: list[AgeBucket] = []
    for label, lo, hi in AGE_BUCKETS:
        if hi is None:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS total
                FROM candidates
                WHERE computed_at_sync_run_id = ?
                  AND reason = 'never_watched_anyone'
                  AND age_days >= ?
                """,
                (run_id, lo),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS total
                FROM candidates
                WHERE computed_at_sync_run_id = ?
                  AND reason = 'never_watched_anyone'
                  AND age_days >= ? AND age_days < ?
                """,
                (run_id, lo, hi),
            ).fetchone()
        out.append(AgeBucket(label=label, count=int(row["n"]), total_bytes=int(row["total"])))
    return out


def instance_cards(conn: sqlite3.Connection) -> list[InstanceCard]:
    rows = conn.execute(
        """
        SELECT i.slug, i.name, i.kind,
               COUNT(ai.id) AS item_count,
               COALESCE(SUM(s.total_bytes), 0) AS total_bytes,
               i.last_seen_ok_at, i.last_error
        FROM instances i
        LEFT JOIN arr_items ai ON ai.instance_id = i.id AND ai.deleted_at IS NULL
        LEFT JOIN (
          SELECT arr_item_id, SUM(size_bytes) AS total_bytes
          FROM arr_files
          WHERE deleted_at IS NULL
          GROUP BY arr_item_id
        ) s ON s.arr_item_id = ai.id
        GROUP BY i.id
        ORDER BY i.kind, i.slug
        """
    ).fetchall()
    return [
        InstanceCard(
            slug=r["slug"],
            name=r["name"],
            kind=r["kind"],
            item_count=int(r["item_count"]),
            total_bytes=int(r["total_bytes"]),
            last_seen_ok_at=r["last_seen_ok_at"],
            last_error=r["last_error"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class FailedStep:
    step_name: str
    error: str | None


def failed_steps(conn: sqlite3.Connection, run_id: int) -> list[FailedStep]:
    rows = conn.execute(
        "SELECT step_name, error_json FROM sync_run_steps "
        "WHERE run_id = ? AND status = 'failed' ORDER BY id",
        (run_id,),
    ).fetchall()
    return [FailedStep(step_name=r["step_name"], error=r["error_json"]) for r in rows]


@dataclass(frozen=True)
class RequesterTotal:
    name: str
    item_count: int
    total_bytes: int
    # Per-bucket counts for the requesters page columns. All counts are taken
    # over the latest run's candidates table.
    never_watched_count: int
    never_watched_bytes: int
    stale_count: int
    stale_bytes: int


def requester_totals(conn: sqlite3.Connection, run_id: int) -> list[RequesterTotal]:
    """Per-requester rollup over the latest run's candidates.

    `item_count` / `total_bytes` count every arr_item attributed to that
    requester, candidate or not. The per-bucket columns count + sum
    candidates whose reason matches that bucket — so users can see at a
    glance "Alex requested 50 things; 30 are never watched, totaling 5 TB."
    """
    rows = conn.execute(
        """
        SELECT
          COALESCE(ra.requester_name, '(no requester)') AS name,
          COUNT(DISTINCT ai.id) AS item_count,
          COALESCE(SUM(s.total_bytes), 0) AS total_bytes,
          SUM(CASE WHEN c.reason = 'never_watched_anyone' THEN 1 ELSE 0 END)
            AS never_watched_count,
          COALESCE(SUM(CASE WHEN c.reason = 'never_watched_anyone'
                            THEN c.size_bytes ELSE 0 END), 0)
            AS never_watched_bytes,
          SUM(CASE WHEN c.reason IN ('stale_finished_anyone',
                                     'stale_partial_anyone') THEN 1 ELSE 0 END)
            AS stale_count,
          COALESCE(SUM(CASE WHEN c.reason IN ('stale_finished_anyone',
                                              'stale_partial_anyone')
                            THEN c.size_bytes ELSE 0 END), 0)
            AS stale_bytes
        FROM arr_items ai
        LEFT JOIN request_attribution ra ON ra.arr_item_id = ai.id
        LEFT JOIN candidates c ON c.arr_item_id = ai.id
            AND c.computed_at_sync_run_id = ?
        LEFT JOIN (
          SELECT arr_item_id, SUM(size_bytes) AS total_bytes
          FROM arr_files
          WHERE deleted_at IS NULL
          GROUP BY arr_item_id
        ) s ON s.arr_item_id = ai.id
        WHERE ai.deleted_at IS NULL
        GROUP BY name
        ORDER BY total_bytes DESC
        """,
        (run_id,),
    ).fetchall()
    return [
        RequesterTotal(
            name=r["name"],
            item_count=int(r["item_count"]),
            total_bytes=int(r["total_bytes"]),
            never_watched_count=int(r["never_watched_count"] or 0),
            never_watched_bytes=int(r["never_watched_bytes"] or 0),
            stale_count=int(r["stale_count"] or 0),
            stale_bytes=int(r["stale_bytes"] or 0),
        )
        for r in rows
    ]


@dataclass(frozen=True)
class TopRequester:
    name: str
    candidate_bytes: int
    candidate_count: int


def top_requesters_by_reclaim(
    conn: sqlite3.Connection, run_id: int, *, limit: int = 5
) -> list[TopRequester]:
    """For the homepage strip: who has the most dead-candidate bytes."""
    rows = conn.execute(
        """
        SELECT
          COALESCE(ra.requester_name, '(no requester)') AS name,
          COALESCE(SUM(c.size_bytes), 0) AS bytes,
          COUNT(*) AS n
        FROM candidates c
        JOIN arr_items ai ON ai.id = c.arr_item_id
        LEFT JOIN request_attribution ra ON ra.arr_item_id = ai.id
        WHERE c.computed_at_sync_run_id = ?
        GROUP BY name
        HAVING bytes > 0
        ORDER BY bytes DESC
        LIMIT ?
        """,
        (run_id, limit),
    ).fetchall()
    return [
        TopRequester(
            name=r["name"],
            candidate_bytes=int(r["bytes"] or 0),
            candidate_count=int(r["n"] or 0),
        )
        for r in rows
    ]
