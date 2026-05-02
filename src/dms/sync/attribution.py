"""Recompute request_attribution per arr_item.

Resolution order (PLAN.md decisions log #4):
  1. requests table (overseerr/seerr) — earliest request per media wins.
  2. parsed tag (single requester tag) → that name.
  3. multiple parsed tags → "multi-requester".
  4. none → NULL (treated as "me" at the display layer).

Computed entirely in SQL — no Python join needed.
"""

from __future__ import annotations

import sqlite3


def recompute_request_attribution(conn: sqlite3.Connection) -> int:
    """Replace request_attribution with freshly computed rows. Returns count."""
    with conn:
        conn.execute("DELETE FROM request_attribution")

        # Step 1: requests table (earliest createdAt per media wins).
        # Match by tmdb_id for movies, tvdb_id for series, fallback to tmdb for series.
        conn.execute(
            """
            INSERT INTO request_attribution (arr_item_id, requester_id, requester_name, source)
            SELECT
                ai.id,
                r.requester_id,
                r.requester_name,
                r.source
            FROM arr_items ai
            JOIN requests r ON
              (ai.kind = 'movie'  AND r.tmdb_id IS NOT NULL AND r.tmdb_id = ai.tmdb_id)
              OR (ai.kind = 'series' AND r.tvdb_id IS NOT NULL AND r.tvdb_id = ai.tvdb_id)
              OR (ai.kind = 'series' AND r.tvdb_id IS NULL AND r.tmdb_id IS NOT NULL
                  AND r.tmdb_id = ai.tmdb_id)
            WHERE ai.deleted_at IS NULL
              AND r.id = (
                SELECT MIN(r2.id) FROM requests r2
                WHERE
                  ((ai.kind = 'movie'  AND r2.tmdb_id = ai.tmdb_id)
                   OR (ai.kind = 'series' AND r2.tvdb_id = ai.tvdb_id)
                   OR (ai.kind = 'series' AND r2.tvdb_id IS NULL AND r2.tmdb_id = ai.tmdb_id))
              )
            ON CONFLICT(arr_item_id) DO NOTHING
            """
        )

        # Step 2 + 3: tags. Only fill items still missing attribution.
        # Single requester tag → use it. Multiple → "multi-requester".
        conn.execute(
            """
            INSERT INTO request_attribution (arr_item_id, requester_id, requester_name, source)
            SELECT
                t.arr_item_id,
                CASE WHEN COUNT(DISTINCT t.parsed_requester_id) = 1
                     THEN MIN(t.parsed_requester_id)
                     ELSE NULL END,
                CASE WHEN COUNT(DISTINCT t.parsed_requester_name) = 1
                     THEN MIN(t.parsed_requester_name)
                     ELSE 'multi-requester' END,
                'tag'
            FROM tags t
            JOIN arr_items ai ON ai.id = t.arr_item_id AND ai.deleted_at IS NULL
            LEFT JOIN request_attribution ra ON ra.arr_item_id = t.arr_item_id
            WHERE ra.arr_item_id IS NULL
              AND t.is_unparseable = 0
              AND t.parsed_requester_name IS NOT NULL
            GROUP BY t.arr_item_id
            """
        )

        cnt = conn.execute("SELECT COUNT(*) FROM request_attribution").fetchone()[0]
    return int(cnt)
