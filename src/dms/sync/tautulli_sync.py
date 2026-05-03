"""Tautulli watch-event sync, page-resumable on row_id.

watch_events.source_row_id is Tautulli's stable PK. We:
- Read the existing MAX(source_row_id) from local DB.
- Walk get_history page by page; insert each page in its own transaction
  with INSERT OR IGNORE so a re-run is safe.
- Persist {tautulli_max_row_id, tautulli_cutoff_unix} in sync_jobs.cursor_json
  AFTER each page lands. A crash mid-backfill can resume from the cursor
  without re-fetching pages we already wrote.

Retention cap (PLAN.md decision #2 / #3): we never request rows older than
HISTORY_RETENTION_YEARS years (default 10). For a fresh install this bounds
the first sync; for incremental syncs it's a no-op because we cut off on
row_id first.

We do NOT tombstone watch_events. Tautulli's own retention/purge is the
source of truth; deleting a play row is unusual and we explicitly mirror
this app to survive Tautulli's pruning.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dms.clients.tautulli import TautulliClient
from dms.config import TautulliConfig
from dms.models import TautulliHistoryRow, TautulliUser
from dms.sync.runs import RunStep, SyncRun, update_cursor

logger = logging.getLogger(__name__)


@dataclass
class TautulliHistoryResult:
    rows_inserted: int
    last_row_id: int | None
    pages_processed: int
    user_count: int


def _max_row_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(source_row_id) FROM watch_events").fetchone()
    return row[0] if row and row[0] is not None else None


def _incremental_after(conn: sqlite3.Connection) -> int | None:
    """Cursor floor for incremental sync.

    PLAN.md asks for a 24-hour overlap window so out-of-order Tautulli
    inserts (or row_id reassignments after a Tautulli DB restore) don't
    silently get dropped. We achieve this by lowering the cursor below the
    smallest row_id we already have in the last 24 hours; INSERT OR IGNORE
    on `source_row_id` handles the duplicates we'll re-fetch.
    """
    overlap = conn.execute(
        """
        SELECT MIN(source_row_id) FROM watch_events
        WHERE started_at IS NOT NULL
          AND started_at >= datetime('now', '-1 day')
        """
    ).fetchone()
    if overlap and overlap[0] is not None:
        return int(overlap[0]) - 1
    # No rows in the last 24h — fall back to the last seen row_id.
    return _max_row_id(conn)


def _retention_cutoff_unix(years: int) -> int:
    """Earliest `started_at` we'll bother fetching, in unix seconds."""
    cutoff = datetime.now(UTC) - timedelta(days=365 * years)
    return int(cutoff.timestamp())


async def sync_tautulli_history(
    conn: sqlite3.Connection,
    run: SyncRun,
    config: TautulliConfig,
    *,
    step: RunStep,
    page_size: int = 500,
    timeout: float = 30.0,
    retention_years: int = 10,
) -> TautulliHistoryResult:
    # First-ever sync: no rows yet, use full retention cap.
    # Incremental sync: lower the cursor by 24h to catch out-of-order rows.
    bootstrap_max = _max_row_id(conn)
    after = _incremental_after(conn) if bootstrap_max is not None else None
    cutoff = _retention_cutoff_unix(retention_years)
    rows_inserted = 0
    pages_processed = 0
    last_row_id: int | None = bootstrap_max
    users: list[TautulliUser] = []

    async with TautulliClient(config, timeout=timeout) as t:
        users = await t.list_users()
        async for page in t.iter_history(
            length=page_size,
            after_row_id=after,
            not_before_unix=None if after is not None else cutoff,
        ):
            page_inserts = 0
            for h in page:
                if _insert_history_row(conn, h):
                    page_inserts += 1
                if last_row_id is None or (h.id is not None and h.id > last_row_id):
                    last_row_id = h.id
            rows_inserted += page_inserts
            pages_processed += 1
            # Checkpoint per page so crashes resume cleanly.
            if last_row_id is not None:
                update_cursor(
                    conn,
                    run,
                    {
                        **run.cursor,
                        "tautulli_max_row_id": last_row_id,
                        "tautulli_cutoff_unix": cutoff,
                        "tautulli_pages_processed": pages_processed,
                    },
                )

    step.items_seen = rows_inserted
    step.items_changed = rows_inserted
    return TautulliHistoryResult(
        rows_inserted=rows_inserted,
        last_row_id=last_row_id,
        pages_processed=pages_processed,
        user_count=len(users),
    )


def _insert_history_row(conn: sqlite3.Connection, h: TautulliHistoryRow) -> bool:
    """INSERT OR IGNORE; return True if a new row was inserted.

    Rows without a Tautulli row_id are silently skipped — `source_row_id`
    is NOT NULL UNIQUE in our schema and represents the resumable cursor.
    Live Tautulli installs do occasionally ship empty `id` fields.
    """
    if h.id is None:
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO watch_events
          (source_row_id, rating_key, parent_rating_key, grandparent_rating_key,
           kind, user_id, user_name, season_number, episode_number,
           started_at, stopped_at, percent_complete, watched_status, play_duration_sec)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            h.id,
            h.rating_key,
            h.parent_rating_key,
            h.grandparent_rating_key,
            h.media_type,
            h.user_id,
            h.user,
            None,  # season_number — denormalized later if needed
            None,  # episode_number
            _seconds_to_iso(h.started),
            _seconds_to_iso(h.stopped),
            h.percent_complete,
            h.watched_status,
            (h.stopped or 0) - (h.started or 0) if h.started and h.stopped else None,
        ),
    )
    return cur.rowcount > 0


def _seconds_to_iso(seconds: int | None) -> str | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


async def fetch_tautulli_users(
    config: TautulliConfig, *, timeout: float = 30.0
) -> list[TautulliUser]:
    """Lightweight standalone helper used by user-identity-map refresh."""
    async with TautulliClient(config, timeout=timeout) as t:
        return await t.list_users()
