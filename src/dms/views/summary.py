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
    never_watched_count: int


def requester_totals(conn: sqlite3.Connection, run_id: int) -> list[RequesterTotal]:
    rows = conn.execute(
        """
        SELECT
          COALESCE(ra.requester_name, '(no requester)') AS name,
          COUNT(DISTINCT ai.id) AS item_count,
          COALESCE(SUM(s.total_bytes), 0) AS total_bytes,
          SUM(CASE WHEN c.reason = 'never_watched_anyone' THEN 1 ELSE 0 END) AS nw
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
            never_watched_count=int(r["nw"] or 0),
        )
        for r in rows
    ]
