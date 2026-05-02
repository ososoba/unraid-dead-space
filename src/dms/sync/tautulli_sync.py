"""Tautulli watch-event sync, resumable on row_id.

watch_events.source_row_id is Tautulli's stable PK. We:
- Read the existing MAX(source_row_id) from local DB.
- Pull pages of get_history; insert each row with INSERT OR IGNORE so a
  re-run is safe (UNIQUE on source_row_id).
- After each successful page, write {tautulli_max_row_id: ...} into the
  run's cursor_json so a crash mid-backfill can resume.

We do NOT tombstone watch_events. Tautulli's own retention/purge is the
source of truth; deleting a play row is unusual and we'd lose history we
explicitly mirror in this app to survive Tautulli's pruning.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC

from dms.clients.tautulli import TautulliClient
from dms.config import TautulliConfig
from dms.models import TautulliHistoryRow, TautulliUser
from dms.sync.runs import RunStep, SyncRun, update_cursor

logger = logging.getLogger(__name__)


@dataclass
class TautulliHistoryResult:
    rows_inserted: int
    last_row_id: int | None
    user_count: int


def _max_row_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(source_row_id) FROM watch_events").fetchone()
    return row[0] if row and row[0] is not None else None


async def sync_tautulli_history(
    conn: sqlite3.Connection,
    run: SyncRun,
    config: TautulliConfig,
    *,
    step: RunStep,
    page_size: int = 500,
    timeout: float = 30.0,
) -> TautulliHistoryResult:
    after = _max_row_id(conn)
    rows_inserted = 0
    last_row_id: int | None = after
    users: list[TautulliUser] = []

    async with TautulliClient(config, timeout=timeout) as t:
        users = await t.list_users()
        history = await t.history(length=page_size, after_row_id=after)

    for h in history:
        if _insert_history_row(conn, h):
            rows_inserted += 1
        if last_row_id is None or h.id > last_row_id:
            last_row_id = h.id

    if last_row_id is not None:
        update_cursor(conn, run, {**run.cursor, "tautulli_max_row_id": last_row_id})

    step.items_seen = len(history)
    step.items_changed = rows_inserted
    return TautulliHistoryResult(
        rows_inserted=rows_inserted,
        last_row_id=last_row_id,
        user_count=len(users),
    )


def _insert_history_row(conn: sqlite3.Connection, h: TautulliHistoryRow) -> bool:
    """INSERT OR IGNORE; return True if a new row was inserted."""
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
    from datetime import datetime

    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


async def fetch_tautulli_users(
    config: TautulliConfig, *, timeout: float = 30.0
) -> list[TautulliUser]:
    """Lightweight standalone helper used by user-identity-map refresh."""
    async with TautulliClient(config, timeout=timeout) as t:
        return await t.list_users()
