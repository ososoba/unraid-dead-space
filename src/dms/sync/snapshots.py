"""Capture dashboard rollup numbers over time.

Runs at the end of every successful sync (after candidate computation).
Stores one row per reason plus a special `TOTAL` row for the headline
reclaim figure (DISTINCT arr_item, MAX size — same dedup as the
homepage's headline card).

Idempotent per `sync_run_id` via the UNIQUE constraint, so re-running
the sync (or just the candidates step) doesn't duplicate snapshots.
Old snapshots beyond `retention_days` are pruned in the same call.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

# Pseudo-reason key for the headline rollup (matches the home page card).
TOTAL_KEY = "TOTAL"


def take_snapshot(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    retention_days: int = 365,
) -> int:
    """Persist this run's dashboard numbers. Returns rows inserted."""
    existing = conn.execute(
        "SELECT 1 FROM dashboard_snapshots WHERE sync_run_id = ? LIMIT 1",
        (run_id,),
    ).fetchone()
    if existing:
        return 0

    # Headline: DISTINCT arr_item / plex_item with MAX size (a single arr_item
    # can match many reasons; raw SUM would over-count). Same shape as
    # views.summary.headline_reclaim_bytes.
    total_row = conn.execute(
        """
        SELECT
          COUNT(DISTINCT COALESCE(arr_item_id, -plex_item_id)) AS items,
          COALESCE(SUM(max_size), 0) AS total
        FROM (
          SELECT arr_item_id, plex_item_id, MAX(size_bytes) AS max_size
          FROM candidates
          WHERE computed_at_sync_run_id = ?
          GROUP BY arr_item_id, plex_item_id
        ) sub
        """,
        (run_id,),
    ).fetchone()

    rows: list[tuple[int, str, int, int]] = [
        (run_id, TOTAL_KEY, int(total_row["items"] or 0), int(total_row["total"] or 0))
    ]

    per_reason = conn.execute(
        """
        SELECT reason, COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS total
        FROM candidates
        WHERE computed_at_sync_run_id = ?
        GROUP BY reason
        """,
        (run_id,),
    ).fetchall()
    for r in per_reason:
        rows.append((run_id, r["reason"], int(r["n"]), int(r["total"])))

    days = max(1, int(retention_days))  # validated for the f-string below
    with conn:
        conn.executemany(
            "INSERT INTO dashboard_snapshots "
            "(sync_run_id, reason, item_count, total_bytes) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        # Prune. We can't bind a literal inside datetime(), so this uses
        # the int-validated `days` safely.
        conn.execute(
            f"DELETE FROM dashboard_snapshots WHERE taken_at < datetime('now', '-{days} days')"
        )

    logger.info("dashboard snapshot for run #%d: %d rows", run_id, len(rows))
    return len(rows)
