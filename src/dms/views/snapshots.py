"""Read helpers for dashboard_snapshots — drives the trend strip + the
per-card "since last sync" delta indicators on the homepage.

`reason='TOTAL'` is the headline (DISTINCT-arr-item, MAX-size dedup);
all other reasons are raw per-bucket counts/sums matching the cards.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from dms.sync.snapshots import TOTAL_KEY


@dataclass(frozen=True)
class SnapshotPoint:
    sync_run_id: int
    taken_at: str
    item_count: int
    total_bytes: int


@dataclass(frozen=True)
class Delta:
    """Difference between the latest snapshot for a reason and the one
    immediately before it. `pct` is None when the previous total was 0
    (avoid div-by-zero; the UI shows '—' in that case)."""

    bytes_delta: int
    count_delta: int
    pct: float | None


@dataclass(frozen=True)
class ReasonStat:
    reason: str
    latest: SnapshotPoint
    previous: SnapshotPoint | None
    delta: Delta | None  # None when no previous snapshot exists yet


def _row(row: sqlite3.Row) -> SnapshotPoint:
    return SnapshotPoint(
        sync_run_id=int(row["sync_run_id"]),
        taken_at=str(row["taken_at"]),
        item_count=int(row["item_count"]),
        total_bytes=int(row["total_bytes"]),
    )


def _delta(curr: SnapshotPoint, prev: SnapshotPoint | None) -> Delta | None:
    if prev is None:
        return None
    bytes_delta = curr.total_bytes - prev.total_bytes
    count_delta = curr.item_count - prev.item_count
    pct = (bytes_delta / prev.total_bytes * 100) if prev.total_bytes else None
    return Delta(bytes_delta=bytes_delta, count_delta=count_delta, pct=pct)


def latest_with_delta(conn: sqlite3.Connection, reason: str) -> ReasonStat | None:
    """Pull the two most recent snapshots for `reason` and compute the delta."""
    rows = conn.execute(
        """
        SELECT sync_run_id, taken_at, item_count, total_bytes
        FROM dashboard_snapshots
        WHERE reason = ?
        ORDER BY id DESC
        LIMIT 2
        """,
        (reason,),
    ).fetchall()
    if not rows:
        return None
    latest = _row(rows[0])
    previous = _row(rows[1]) if len(rows) > 1 else None
    return ReasonStat(
        reason=reason, latest=latest, previous=previous, delta=_delta(latest, previous)
    )


def latest_with_delta_many(
    conn: sqlite3.Connection, reasons: Iterable[str]
) -> dict[str, ReasonStat]:
    """Same as `latest_with_delta` but for several reasons in one shot."""
    return {r: stat for r in reasons if (stat := latest_with_delta(conn, r))}


def series(
    conn: sqlite3.Connection,
    reason: str = TOTAL_KEY,
    *,
    limit: int = 30,
) -> list[SnapshotPoint]:
    """The N most recent snapshots for a reason, oldest-first (chart-friendly)."""
    rows = conn.execute(
        """
        SELECT sync_run_id, taken_at, item_count, total_bytes
        FROM dashboard_snapshots
        WHERE reason = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (reason, limit),
    ).fetchall()
    return [_row(r) for r in reversed(rows)]
