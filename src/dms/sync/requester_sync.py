"""Overseerr / Seerr / Jellyseerr request sync + user-identity-map refresh.

Pulls users + requests from each configured requester instance, upserts
into the unified `requests` table (source column distinguishes flavor),
and refreshes `user_identity_map` against Tautulli users.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from dms.clients.requester import RequesterClient
from dms.config import RequesterConfig
from dms.identity import map_requesters_to_tautulli
from dms.models import RequesterUser, TautulliUser
from dms.sync.runs import RunStep
from dms.sync.upsert import upsert

logger = logging.getLogger(__name__)


@dataclass
class RequesterSyncResult:
    request_count: int
    user_count: int


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def sync_requester_instance(
    conn: sqlite3.Connection,
    instance: RequesterConfig,
    *,
    step: RunStep,
    timeout: float = 30.0,
) -> tuple[RequesterSyncResult, list[RequesterUser]]:
    requests_seen = 0
    users: list[RequesterUser] = []

    async with RequesterClient(instance, timeout=timeout) as client:
        users = await client.list_users()
        records = await client.list_requests()

    for rec in records:
        media = rec.media
        requester = rec.requested_by
        upsert(
            conn,
            "requests",
            {
                "source": instance.source,
                "source_request_id": str(rec.id),
                "media_kind": media.media_type if media else None,
                "tmdb_id": media.tmdb_id if media else None,
                "tvdb_id": media.tvdb_id if media else None,
                "requester_id": requester.id if requester else None,
                "requester_name": (
                    requester.display_name or requester.username if requester else None
                ),
                "requested_at": _iso(rec.created_at),
                "status": str(rec.status) if rec.status is not None else None,
            },
            conflict_keys=("source", "source_request_id"),
        )
        requests_seen += 1

    step.items_seen = requests_seen
    step.items_changed = requests_seen
    return RequesterSyncResult(request_count=requests_seen, user_count=len(users)), users


def refresh_user_identity_map(
    conn: sqlite3.Connection,
    requester_users_by_source: dict[str, list[RequesterUser]],
    tautulli_users: list[TautulliUser],
) -> int:
    """Recompute user_identity_map from latest pulls.

    Manual mappings (`match_method = 'manual'`) are preserved — we never
    overwrite them. Auto mappings get refreshed.
    """
    rows_changed = 0
    now = datetime.now(UTC).isoformat()

    for source, requesters in requester_users_by_source.items():
        mappings = map_requesters_to_tautulli(requesters, tautulli_users)
        for m in mappings:
            existing = conn.execute(
                "SELECT id, match_method FROM user_identity_map "
                "WHERE requester_source = ? AND requester_id = ?",
                (source, m.requester_id),
            ).fetchone()
            if existing and existing["match_method"] == "manual":
                continue  # never clobber a user-set mapping
            upsert(
                conn,
                "user_identity_map",
                {
                    "requester_source": source,
                    "requester_id": m.requester_id,
                    "requester_name": m.requester_name,
                    "tautulli_user_id": m.tautulli_user_id,
                    "tautulli_user_name": m.tautulli_user_name,
                    "plex_username": None,
                    "match_method": m.method,
                    "confidence": m.confidence,
                    "updated_at": now,
                },
                conflict_keys=("requester_source", "requester_id"),
            )
            rows_changed += 1
    return rows_changed
