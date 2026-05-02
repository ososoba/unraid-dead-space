"""Sync lock with heartbeat + TTL-based stale recovery.

A single named lock (`global_sync` by default) prevents concurrent sync
runs. The owner refreshes `heartbeat_at` periodically; if a process
crashes mid-sync, the next run can steal the lock once the heartbeat is
older than `ttl_minutes`.

Stale recovery is audit-logged so we can spot crash patterns later.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DEFAULT_LOCK_NAME = "global_sync"


class LockHeldError(RuntimeError):
    """Raised when a fresh lock is held by someone else."""


@dataclass(frozen=True)
class LockHandle:
    name: str
    owner: str
    acquired_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def acquire(
    conn: sqlite3.Connection,
    *,
    name: str = DEFAULT_LOCK_NAME,
    ttl_minutes: int = 120,
    owner: str | None = None,
) -> LockHandle:
    """Acquire the named lock or raise LockHeldError.

    If the existing lock's heartbeat is older than `ttl_minutes`, it is
    stolen and a `stale_lock_recovered` row is written to `audit_log`.
    """
    owner = owner or f"pid-{uuid.uuid4().hex[:8]}"
    now = _now()
    cutoff = now - timedelta(minutes=ttl_minutes)

    # Atomic-ish: SQLite serializes writes; we use an explicit transaction.
    with conn:
        existing = conn.execute(
            "SELECT name, owner, acquired_at, heartbeat_at FROM sync_locks WHERE name = ?",
            (name,),
        ).fetchone()

        if existing:
            prev_heartbeat = _parse_iso(existing["heartbeat_at"])
            if prev_heartbeat is not None and prev_heartbeat >= cutoff:
                raise LockHeldError(
                    f"lock {name!r} held by {existing['owner']!r}, "
                    f"heartbeat {existing['heartbeat_at']}"
                )
            # Stale — steal it and audit.
            conn.execute(
                "DELETE FROM sync_locks WHERE name = ?",
                (name,),
            )
            conn.execute(
                "INSERT INTO audit_log (actor, action, target, details_json) VALUES (?, ?, ?, ?)",
                (
                    owner,
                    "stale_lock_recovered",
                    name,
                    json.dumps(
                        {
                            "previous_owner": existing["owner"],
                            "previous_heartbeat_at": existing["heartbeat_at"],
                            "ttl_minutes": ttl_minutes,
                        }
                    ),
                ),
            )

        conn.execute(
            "INSERT INTO sync_locks (name, acquired_at, heartbeat_at, owner) VALUES (?, ?, ?, ?)",
            (name, _iso(now), _iso(now), owner),
        )

    return LockHandle(name=name, owner=owner, acquired_at=now)


def heartbeat(conn: sqlite3.Connection, handle: LockHandle) -> bool:
    """Refresh the heartbeat. Returns False if the lock was stolen."""
    now = _now()
    with conn:
        cur = conn.execute(
            "UPDATE sync_locks SET heartbeat_at = ? WHERE name = ? AND owner = ?",
            (_iso(now), handle.name, handle.owner),
        )
    return cur.rowcount > 0


def release(conn: sqlite3.Connection, handle: LockHandle) -> bool:
    """Release the lock. Returns False if it was already gone (stolen)."""
    with conn:
        cur = conn.execute(
            "DELETE FROM sync_locks WHERE name = ? AND owner = ?",
            (handle.name, handle.owner),
        )
    return cur.rowcount > 0
