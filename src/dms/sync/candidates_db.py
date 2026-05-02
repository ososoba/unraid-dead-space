"""DB-based candidate engine — writes to `candidates` for the current run.

Computes all eight reasons in SQL by joining arr_items + watch_state +
arr_files (for size_bytes) + plex_items / plex_media_files (for orphans).

Old candidate rows from older sync runs are kept (last 3 retained) so the
UI can diff between runs while never showing stale buckets — the UI
filters by `computed_at_sync_run_id = (latest succeeded)`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def compute_candidates(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    never_watched_days: int = 90,
    stale_days: int = 180,
    keep_last_n_runs: int = 3,
) -> int:
    """Rebuild candidates for `run_id`. Returns rows inserted."""
    with conn:
        # Per-arr-item size from arr_files. For movies = single file; for series
        # = sum of all episode files. Compute as a CTE-friendly subquery view.
        conn.execute("DROP VIEW IF EXISTS v_arr_item_size")
        conn.execute(
            """
            CREATE TEMP VIEW v_arr_item_size AS
            SELECT ai.id AS arr_item_id, COALESCE(SUM(af.size_bytes), 0) AS size_bytes
            FROM arr_items ai
            LEFT JOIN arr_files af
              ON af.arr_item_id = ai.id AND af.deleted_at IS NULL
            WHERE ai.deleted_at IS NULL
            GROUP BY ai.id
            """
        )

        params = {
            "run_id": run_id,
            "never_days": never_watched_days,
            "stale_days": stale_days,
        }

        # 1. never_watched_anyone
        conn.execute(
            """
            INSERT INTO candidates
              (arr_item_id, plex_item_id, reason, scope, size_bytes, age_days,
               last_played_at, confidence, computed_at_sync_run_id)
            SELECT
              ai.id, NULL, 'never_watched_anyone', 'anyone',
              s.size_bytes,
              CAST((julianday('now') - julianday(ai.added_at)) AS INTEGER),
              NULL, 'high', :run_id
            FROM arr_items ai
            JOIN v_arr_item_size s ON s.arr_item_id = ai.id
            LEFT JOIN watch_state ws ON ws.arr_item_id = ai.id
            LEFT JOIN ignore_rules ir ON ir.arr_item_id = ai.id
            WHERE ai.deleted_at IS NULL
              AND ai.ignored_local = 0
              AND ir.id IS NULL
              AND COALESCE(ws.has_any_play, 0) = 0
              AND ai.added_at IS NOT NULL
              AND CAST((julianday('now') - julianday(ai.added_at)) AS INTEGER) > :never_days
            """,
            params,
        )

        # 2. never_watched_requester
        conn.execute(
            """
            INSERT INTO candidates
              (arr_item_id, plex_item_id, reason, scope, size_bytes, age_days,
               last_played_at, confidence, computed_at_sync_run_id)
            SELECT
              ai.id, NULL, 'never_watched_requester', 'requester',
              s.size_bytes,
              CAST((julianday('now') - julianday(ai.added_at)) AS INTEGER),
              NULL,
              CASE WHEN COALESCE(ws.requester_mapping_confidence, 'low') = 'low'
                   THEN 'low' ELSE 'high' END,
              :run_id
            FROM arr_items ai
            JOIN v_arr_item_size s ON s.arr_item_id = ai.id
            JOIN request_attribution ra ON ra.arr_item_id = ai.id
            LEFT JOIN watch_state ws ON ws.arr_item_id = ai.id
            LEFT JOIN ignore_rules ir ON ir.arr_item_id = ai.id
            WHERE ai.deleted_at IS NULL
              AND ai.ignored_local = 0
              AND ir.id IS NULL
              AND ra.requester_name IS NOT NULL
              AND COALESCE(ws.has_requester_play, 0) = 0
              AND ai.added_at IS NOT NULL
              AND CAST((julianday('now') - julianday(ai.added_at)) AS INTEGER) > :never_days
            """,
            params,
        )

        # 3+4. stale_finished_anyone / stale_partial_anyone
        conn.execute(
            """
            INSERT INTO candidates
              (arr_item_id, plex_item_id, reason, scope, size_bytes, age_days,
               last_played_at, confidence, computed_at_sync_run_id)
            SELECT
              ai.id, NULL,
              CASE WHEN ws.is_finished_anyone = 1
                   THEN 'stale_finished_anyone' ELSE 'stale_partial_anyone' END,
              'anyone',
              s.size_bytes,
              CAST((julianday('now') - julianday(ai.added_at)) AS INTEGER),
              ws.last_played_at_anyone, 'high', :run_id
            FROM arr_items ai
            JOIN v_arr_item_size s ON s.arr_item_id = ai.id
            JOIN watch_state ws ON ws.arr_item_id = ai.id
            LEFT JOIN ignore_rules ir ON ir.arr_item_id = ai.id
            WHERE ai.deleted_at IS NULL
              AND ai.ignored_local = 0
              AND ir.id IS NULL
              AND ws.has_any_play = 1
              AND ws.last_played_at_anyone IS NOT NULL
              AND CAST((julianday('now') - julianday(ws.last_played_at_anyone)) AS INTEGER)
                  > :stale_days
            """,
            params,
        )

        # 5+6. stale_finished_requester / stale_partial_requester
        conn.execute(
            """
            INSERT INTO candidates
              (arr_item_id, plex_item_id, reason, scope, size_bytes, age_days,
               last_played_at, confidence, computed_at_sync_run_id)
            SELECT
              ai.id, NULL,
              CASE WHEN ws.is_finished_requester = 1
                   THEN 'stale_finished_requester' ELSE 'stale_partial_requester' END,
              'requester',
              s.size_bytes,
              CAST((julianday('now') - julianday(ai.added_at)) AS INTEGER),
              ws.last_played_at_requester,
              CASE WHEN COALESCE(ws.requester_mapping_confidence, 'low') = 'low'
                   THEN 'low' ELSE 'high' END,
              :run_id
            FROM arr_items ai
            JOIN v_arr_item_size s ON s.arr_item_id = ai.id
            JOIN watch_state ws ON ws.arr_item_id = ai.id
            JOIN request_attribution ra ON ra.arr_item_id = ai.id
            LEFT JOIN ignore_rules ir ON ir.arr_item_id = ai.id
            WHERE ai.deleted_at IS NULL
              AND ai.ignored_local = 0
              AND ir.id IS NULL
              AND ra.requester_name IS NOT NULL
              AND ws.has_requester_play = 1
              AND ws.last_played_at_requester IS NOT NULL
              AND CAST((julianday('now') - julianday(ws.last_played_at_requester)) AS INTEGER)
                  > :stale_days
            """,
            params,
        )

        # 7. orphan_arr_no_plex — arr_items with no plex_item match by external ID.
        conn.execute(
            """
            INSERT INTO candidates
              (arr_item_id, plex_item_id, reason, scope, size_bytes, age_days,
               last_played_at, confidence, computed_at_sync_run_id)
            SELECT
              ai.id, NULL, 'orphan_arr_no_plex', 'anyone',
              s.size_bytes, NULL, NULL, 'high', :run_id
            FROM arr_items ai
            JOIN v_arr_item_size s ON s.arr_item_id = ai.id
            LEFT JOIN ignore_rules ir ON ir.arr_item_id = ai.id
            WHERE ai.deleted_at IS NULL
              AND ai.ignored_local = 0
              AND ir.id IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM plex_items pi
                WHERE pi.deleted_at IS NULL
                  AND ((ai.tmdb_id IS NOT NULL AND pi.tmdb_id = ai.tmdb_id)
                    OR (ai.tvdb_id IS NOT NULL AND pi.tvdb_id = ai.tvdb_id)
                    OR (ai.imdb_id IS NOT NULL AND pi.imdb_id = ai.imdb_id))
              )
            """,
            params,
        )

        # 8. orphan_plex_no_arr — plex_items with no arr_item match.
        conn.execute(
            """
            INSERT INTO candidates
              (arr_item_id, plex_item_id, plex_media_file_id, reason, scope,
               size_bytes, age_days, last_played_at, confidence,
               computed_at_sync_run_id)
            SELECT
              NULL, pi.id, pmf.id, 'orphan_plex_no_arr', 'anyone',
              COALESCE(pmf.size_bytes, 0), NULL, NULL, 'high', :run_id
            FROM plex_items pi
            LEFT JOIN plex_media_files pmf
              ON pmf.plex_item_id = pi.id AND pmf.deleted_at IS NULL
            WHERE pi.deleted_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM arr_items ai
                WHERE ai.deleted_at IS NULL
                  AND ((pi.tmdb_id IS NOT NULL AND ai.tmdb_id = pi.tmdb_id)
                    OR (pi.tvdb_id IS NOT NULL AND ai.tvdb_id = pi.tvdb_id)
                    OR (pi.imdb_id IS NOT NULL AND ai.imdb_id = pi.imdb_id))
              )
            """,
            params,
        )

        # Prune all but last N successful runs' candidates.
        _prune_old_runs(conn, run_id=run_id, keep=keep_last_n_runs)

        cnt = conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE computed_at_sync_run_id = ?",
            (run_id,),
        ).fetchone()[0]
    return int(cnt)


def _prune_old_runs(conn: sqlite3.Connection, *, run_id: int, keep: int) -> None:
    """Keep candidates only for the latest N distinct sync_run_ids."""
    rows = conn.execute(
        "SELECT DISTINCT computed_at_sync_run_id FROM candidates "
        "ORDER BY computed_at_sync_run_id DESC LIMIT ?",
        (keep,),
    ).fetchall()
    keep_ids = {r[0] for r in rows}
    keep_ids.add(run_id)
    if not keep_ids:
        return
    placeholders = ",".join("?" * len(keep_ids))
    conn.execute(
        f"DELETE FROM candidates WHERE computed_at_sync_run_id NOT IN ({placeholders})",
        list(keep_ids),
    )
