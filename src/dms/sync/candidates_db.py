"""DB-based candidate engine — writes to `candidates` for the current run.

Computes all eight reasons in SQL by joining arr_items + watch_state +
arr_files (for size_bytes) + plex_items / plex_media_files (for orphans).

Old candidate rows from older sync runs are kept (last 3 retained) so the
UI can diff between runs while never showing stale buckets — the UI
filters by `computed_at_sync_run_id = (latest succeeded)`.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _log_series_size_coverage(conn: sqlite3.Connection) -> None:
    """Surface a warning when series orphan detection has degraded to
    external-ID matching because Plex isn't returning aggregate file sizes.

    See `7b` SQL block for the underlying limitation. We log a single
    INFO line per sync if >50% of series-section plex_items have no
    media-file size data, because that's when the file-level fix from
    PLAN.md decision #19 is effectively a no-op.
    """
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN COALESCE(pmf.total, 0) > 0 THEN 1 ELSE 0 END) AS with_size,
          COUNT(*) AS total
        FROM plex_items pi
        LEFT JOIN (
          SELECT plex_item_id, SUM(size_bytes) AS total
          FROM plex_media_files
          WHERE deleted_at IS NULL
          GROUP BY plex_item_id
        ) pmf ON pmf.plex_item_id = pi.id
        WHERE pi.deleted_at IS NULL AND pi.kind IN ('show', 'series')
        """
    ).fetchone()
    total = int(row["total"] or 0)
    if total == 0:
        return
    with_size = int(row["with_size"] or 0)
    if with_size * 2 < total:
        logger.info(
            "series orphan detection degraded: %d/%d Plex shows have no "
            "aggregate file size — split-quality Sonarr instances may not "
            "be flagged. v2 episode-level Plex inventory will fix this.",
            total - with_size,
            total,
        )


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

        # 7a. orphan_arr_no_plex (movies) — file-level match.
        # PLAN.md decision #19: each Arr movie file must have a Plex media
        # file with the same external ID AND a similar size (within 5%).
        # This catches the 4K-vs-1080p split: same tmdb_id, different sizes.
        # Series stay external-ID for now (per-episode matching = v2).
        conn.execute(
            """
            INSERT INTO candidates
              (arr_item_id, plex_item_id, reason, scope, size_bytes, age_days,
               last_played_at, confidence, computed_at_sync_run_id)
            SELECT
              ai.id, NULL, 'orphan_arr_no_plex', 'anyone',
              af.size_bytes, NULL, NULL, 'high', :run_id
            FROM arr_items ai
            JOIN arr_files af
              ON af.arr_item_id = ai.id
              AND af.deleted_at IS NULL
              AND af.kind = 'movie'
            LEFT JOIN ignore_rules ir ON ir.arr_item_id = ai.id
            WHERE ai.kind = 'movie'
              AND ai.deleted_at IS NULL
              AND ai.ignored_local = 0
              AND ir.id IS NULL
              AND NOT EXISTS (
                SELECT 1
                FROM plex_items pi
                JOIN plex_media_files pmf
                  ON pmf.plex_item_id = pi.id AND pmf.deleted_at IS NULL
                WHERE pi.deleted_at IS NULL
                  AND ((pi.tmdb_id IS NOT NULL AND pi.tmdb_id = ai.tmdb_id)
                    OR (pi.imdb_id IS NOT NULL AND pi.imdb_id = ai.imdb_id))
                  AND (
                    pmf.size_bytes = 0
                    OR af.size_bytes = 0
                    OR ABS(pmf.size_bytes - af.size_bytes) < af.size_bytes * 0.05
                  )
              )
            """,
            params,
        )

        # 7b. orphan_arr_no_plex (series) — best-effort file-level by aggregate.
        #
        # KNOWN LIMITATION (PLAN.md v2 item: episode-level Plex inventory):
        # The current Plex sync only calls get_metadata() on the rating keys
        # returned by get_library_media_info, which for TV sections are
        # SHOW-level keys. Show metadata in Tautulli's media_info[] array is
        # usually empty (parts/file are episode-level concepts). So in
        # practice `pmf_total IS NULL` for most shows, and the lenient branch
        # below collapses this back to external-ID-only matching for series.
        #
        # When `pmf_total > 0` (rare today, common after the v2 episode-level
        # inventory pass), the size-sum comparison kicks in and catches
        # split-quality Sonarr instances (1080p vs 4K, Plex has only one).
        #
        # Why we keep the lenient fallback today: tightening it would
        # generate false-positive orphans for *every* series with Plex's
        # show-level metadata returning no parts (i.e. nearly all series
        # right now). Better to under-report than to drown the user in noise.
        #
        # Movie orphan detection (7a above) does NOT have this limitation:
        # movie metadata DOES populate media_info[].parts[] reliably.
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
            WHERE ai.kind = 'series'
              AND ai.deleted_at IS NULL
              AND ai.ignored_local = 0
              AND ir.id IS NULL
              AND NOT EXISTS (
                SELECT 1
                FROM plex_items pi
                LEFT JOIN (
                  SELECT plex_item_id, SUM(size_bytes) AS total
                  FROM plex_media_files
                  WHERE deleted_at IS NULL
                  GROUP BY plex_item_id
                ) pmf_total ON pmf_total.plex_item_id = pi.id
                WHERE pi.deleted_at IS NULL
                  AND ((ai.tvdb_id IS NOT NULL AND pi.tvdb_id = ai.tvdb_id)
                    OR (ai.tmdb_id IS NOT NULL AND pi.tmdb_id = ai.tmdb_id)
                    OR (ai.imdb_id IS NOT NULL AND pi.imdb_id = ai.imdb_id))
                  AND (
                    -- No size data on either side → fall back to ID match.
                    s.size_bytes = 0
                    OR pmf_total.total IS NULL
                    OR pmf_total.total = 0
                    OR ABS(pmf_total.total - s.size_bytes) < s.size_bytes * 0.05
                  )
              )
            """,
            params,
        )

        # 8. orphan_plex_no_arr — Plex files that no Arr knows about.
        # File-level: a plex_media_file is orphan if no arr_file matches by
        # external ID + size tolerance. We surface ONE candidate per
        # plex_media_file so size accounting is accurate.
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
            JOIN plex_media_files pmf
              ON pmf.plex_item_id = pi.id AND pmf.deleted_at IS NULL
            WHERE pi.deleted_at IS NULL
              AND NOT EXISTS (
                SELECT 1
                FROM arr_items ai
                JOIN arr_files af
                  ON af.arr_item_id = ai.id AND af.deleted_at IS NULL
                WHERE ai.deleted_at IS NULL
                  AND ((pi.tmdb_id IS NOT NULL AND ai.tmdb_id = pi.tmdb_id)
                    OR (pi.tvdb_id IS NOT NULL AND ai.tvdb_id = pi.tvdb_id)
                    OR (pi.imdb_id IS NOT NULL AND ai.imdb_id = pi.imdb_id))
                  AND (
                    af.size_bytes = 0
                    OR pmf.size_bytes = 0
                    OR ABS(af.size_bytes - pmf.size_bytes) < pmf.size_bytes * 0.05
                  )
              )
            """,
            params,
        )

        # Prune all but last N successful runs' candidates.
        _prune_old_runs(conn, run_id=run_id, keep=keep_last_n_runs)

        # Surface the series-orphan limitation if it's biting this install.
        _log_series_size_coverage(conn)

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
