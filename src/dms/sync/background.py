"""Fire-and-forget sync runner for background tasks.

The HTTP request that triggers a sync should not block until the sync
finishes. `start_background_sync` opens its own DB connection, calls
`run_sync`, and updates the in-process registry so the live progress UI
can poll status.
"""

from __future__ import annotations

import asyncio
import logging
from os import PathLike
from typing import Any

from dms.config import AppConfig
from dms.db import connect
from dms.sync.locks import LockHeldError
from dms.sync.runner import run_sync
from dms.sync.runs import JobKind

logger = logging.getLogger(__name__)


# A weak in-process registry of currently-running tasks per db_path. We
# don't need durability here — the sync_locks row in DB is the source of
# truth. This just lets the request handler avoid spawning a duplicate
# task for the same DB.
_active: dict[str, asyncio.Task] = {}


async def _run_and_record(
    db_path: str | PathLike[str],
    config: AppConfig,
    *,
    kind: JobKind,
    requested_by: str,
) -> dict[str, Any]:
    """Open a fresh connection, run sync, return summary as dict.

    Errors are logged and swallowed (the request that started this is
    long gone). The DB-level sync_locks + sync_jobs rows are how status
    is communicated downstream.
    """
    conn = connect(db_path)
    try:
        try:
            summary = await run_sync(conn, config, kind=kind, requested_by=requested_by)
        except LockHeldError as exc:
            logger.warning("sync skipped: %s", exc)
            return {"status": "skipped", "error": str(exc)}
        return {
            "run_id": summary.run_id,
            "status": summary.status,
            "candidate_rows": summary.candidate_rows,
            "watch_state_rows": summary.watch_state_rows,
        }
    except Exception:
        logger.exception("background sync failed")
        return {"status": "failed"}
    finally:
        conn.close()


def start_background_sync(
    db_path: str | PathLike[str],
    config: AppConfig,
    *,
    kind: JobKind = "manual",
    requested_by: str = "ui",
) -> bool:
    """Start a background sync if one isn't already in flight for this DB.

    Returns True if a new task was spawned, False if the previous one is
    still running. Lock conflicts at the DB level are handled inside
    `_run_and_record` (skipped + logged); this in-process check is just
    to avoid silly duplicate task spawns from rapid double-clicks.
    """
    key = str(db_path)
    existing = _active.get(key)
    if existing is not None and not existing.done():
        return False
    task = asyncio.create_task(
        _run_and_record(db_path, config, kind=kind, requested_by=requested_by)
    )
    _active[key] = task
    return True


def is_running(db_path: str | PathLike[str]) -> bool:
    task = _active.get(str(db_path))
    return task is not None and not task.done()
