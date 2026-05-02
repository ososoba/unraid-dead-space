"""Recompute watch_state per arr_item using SQL joins.

Joins:
  - arr_items.tmdb_id / tvdb_id / imdb_id → plex_items (canonical Plex match)
  - For movies: watch_events.rating_key = plex_items.rating_key
  - For series: watch_events.grandparent_rating_key = plex_items.rating_key
  - request_attribution → user_identity_map (resolves requester to Tautulli user)
  - Threshold filter: watch_events.percent_complete >= threshold

A single arr_item can match multiple plex_items in mixed-quality setups
(e.g. 4K + 1080p in different Plex sections, same tmdb/tvdb id). We
GROUP BY arr_item_id and use MAX/aggregate to coalesce so the watch_state
PK invariant holds. v2 will switch to a UNION across rating_keys for
exact episode coverage; for now MAX gives a correct upper bound which is
always sufficient for the candidate engine's "is this watched" question.
"""

from __future__ import annotations

import sqlite3


def recompute_watch_state(
    conn: sqlite3.Connection,
    *,
    threshold_movies_pct: int = 80,
    threshold_episodes_pct: int = 80,
    specials_mode: str = "ignore",
) -> int:
    """Rebuild watch_state from scratch for every live arr_item."""
    include_specials = 1 if specials_mode == "include" else 0
    with conn:
        conn.execute("DELETE FROM watch_state")

        # Movies: aggregate across multiple plex_items per arr_item via GROUP BY.
        conn.execute(
            """
            INSERT INTO watch_state
              (arr_item_id, has_any_play, has_requester_play,
               total_episodes,
               episodes_watched_count_anyone, episodes_watched_count_requester,
               episode_coverage_pct_anyone, episode_coverage_pct_requester,
               last_played_at_anyone, last_played_at_requester,
               is_finished_anyone, is_finished_requester,
               requester_mapping_confidence)
            SELECT
              ai.id,
              MAX(CASE WHEN any_count > 0 THEN 1 ELSE 0 END),
              MAX(CASE WHEN req_count > 0 THEN 1 ELSE 0 END),
              NULL, NULL, NULL, NULL, NULL,
              MAX(any_last), MAX(req_last),
              MAX(CASE WHEN any_count > 0 THEN 1 ELSE 0 END),
              MAX(CASE WHEN req_count > 0 THEN 1 ELSE 0 END),
              MAX(uim.confidence)
            FROM arr_items ai
            LEFT JOIN plex_items pi
              ON ((pi.tmdb_id IS NOT NULL AND pi.tmdb_id = ai.tmdb_id)
                  OR (pi.imdb_id IS NOT NULL AND pi.imdb_id = ai.imdb_id))
                 AND pi.deleted_at IS NULL
            LEFT JOIN request_attribution ra ON ra.arr_item_id = ai.id
            LEFT JOIN user_identity_map uim
              ON uim.requester_id = ra.requester_id AND uim.requester_name = ra.requester_name
            LEFT JOIN (
              SELECT we.rating_key,
                     COUNT(*) AS any_count,
                     MAX(we.stopped_at) AS any_last
              FROM watch_events we
              WHERE we.percent_complete >= ?
              GROUP BY we.rating_key
            ) anyone ON anyone.rating_key = pi.rating_key
            LEFT JOIN (
              SELECT we.rating_key, we.user_id,
                     COUNT(*) AS req_count,
                     MAX(we.stopped_at) AS req_last
              FROM watch_events we
              WHERE we.percent_complete >= ?
              GROUP BY we.rating_key, we.user_id
            ) requester
              ON requester.rating_key = pi.rating_key
              AND requester.user_id = uim.tautulli_user_id
            WHERE ai.kind = 'movie' AND ai.deleted_at IS NULL
            GROUP BY ai.id
            """,
            (threshold_movies_pct, threshold_movies_pct),
        )

        # Series: episode coverage. MAX across plex copies (see module docstring).
        conn.execute(
            """
            INSERT INTO watch_state
              (arr_item_id, has_any_play, has_requester_play,
               total_episodes,
               episodes_watched_count_anyone, episodes_watched_count_requester,
               episode_coverage_pct_anyone, episode_coverage_pct_requester,
               last_played_at_anyone, last_played_at_requester,
               is_finished_anyone, is_finished_requester,
               requester_mapping_confidence)
            SELECT
              ai.id,
              MAX(CASE WHEN COALESCE(any_eps, 0) > 0 THEN 1 ELSE 0 END),
              MAX(CASE WHEN COALESCE(req_eps, 0) > 0 THEN 1 ELSE 0 END),
              MAX(total_eps),
              MAX(any_eps), MAX(req_eps),
              CASE WHEN MAX(total_eps) > 0
                   THEN 100.0 * MAX(any_eps) / MAX(total_eps) ELSE NULL END,
              CASE WHEN MAX(total_eps) > 0
                   THEN 100.0 * MAX(req_eps) / MAX(total_eps) ELSE NULL END,
              MAX(any_last), MAX(req_last),
              CASE WHEN MAX(total_eps) > 0 AND MAX(any_eps) >= MAX(total_eps)
                   THEN 1 ELSE 0 END,
              CASE WHEN MAX(total_eps) > 0 AND MAX(req_eps) >= MAX(total_eps)
                   THEN 1 ELSE 0 END,
              MAX(uim.confidence)
            FROM arr_items ai
            LEFT JOIN plex_items pi
              ON ((pi.tvdb_id IS NOT NULL AND pi.tvdb_id = ai.tvdb_id)
                  OR (pi.tmdb_id IS NOT NULL AND pi.tmdb_id = ai.tmdb_id))
                 AND pi.deleted_at IS NULL
            LEFT JOIN request_attribution ra ON ra.arr_item_id = ai.id
            LEFT JOIN user_identity_map uim
              ON uim.requester_id = ra.requester_id AND uim.requester_name = ra.requester_name
            LEFT JOIN (
              SELECT arr_item_id, COUNT(*) AS total_eps
              FROM arr_episodes
              WHERE deleted_at IS NULL
                AND (is_special = 0 OR ? = 1)
              GROUP BY arr_item_id
            ) ec ON ec.arr_item_id = ai.id
            LEFT JOIN (
              SELECT we.grandparent_rating_key,
                     COUNT(DISTINCT we.rating_key) AS any_eps,
                     MAX(we.stopped_at) AS any_last
              FROM watch_events we
              WHERE we.percent_complete >= ?
                AND we.grandparent_rating_key IS NOT NULL
              GROUP BY we.grandparent_rating_key
            ) anyone ON anyone.grandparent_rating_key = pi.rating_key
            LEFT JOIN (
              SELECT we.grandparent_rating_key, we.user_id,
                     COUNT(DISTINCT we.rating_key) AS req_eps,
                     MAX(we.stopped_at) AS req_last
              FROM watch_events we
              WHERE we.percent_complete >= ?
                AND we.grandparent_rating_key IS NOT NULL
              GROUP BY we.grandparent_rating_key, we.user_id
            ) requester
              ON requester.grandparent_rating_key = pi.rating_key
              AND requester.user_id = uim.tautulli_user_id
            WHERE ai.kind = 'series' AND ai.deleted_at IS NULL
            GROUP BY ai.id
            """,
            (include_specials, threshold_episodes_pct, threshold_episodes_pct),
        )

        cnt = conn.execute("SELECT COUNT(*) FROM watch_state").fetchone()[0]
    return int(cnt)
