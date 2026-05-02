"""Plex inventory sync (via Tautulli).

Pulls library_media_info per non-empty section, enriches each item with
guids via get_metadata, and upserts into plex_items. media_info rows
have file_size + container etc., which feed plex_media_files for
file-level orphan detection.

On the first sync this triggers a Tautulli library refresh (refresh=true)
because the cached media_info table is otherwise stale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass

from dms.clients.base import UpstreamError
from dms.clients.tautulli import TautulliClient
from dms.config import TautulliConfig
from dms.identity import resolve_plex_item
from dms.models import TautulliLibraryItem
from dms.sync.runs import RunStep
from dms.sync.upsert import mark_tombstones, upsert

logger = logging.getLogger(__name__)


@dataclass
class PlexSyncResult:
    items_seen: int
    files_seen: int


_PLEX_KIND_BY_SECTION = {"movie": "movie", "show": "show"}


def _section_kind(section_type: str | None) -> str | None:
    if not section_type:
        return None
    return _PLEX_KIND_BY_SECTION.get(section_type.lower())


async def sync_plex_inventory(
    conn: sqlite3.Connection,
    config: TautulliConfig,
    *,
    run_id: int,
    step: RunStep,
    concurrency: int = 4,
    timeout: float = 30.0,
) -> PlexSyncResult:
    items_seen = 0
    files_seen = 0
    async with TautulliClient(config, timeout=timeout) as t:
        libs = await t.list_libraries()
        for lib in libs:
            sid = lib.get("section_id")
            count = lib.get("count")
            if not sid or count in (0, "0"):
                continue
            try:
                section_id = int(sid)
            except (TypeError, ValueError):
                logger.warning("non-int section_id %r — skipping", sid)
                continue
            kind = _section_kind(lib.get("section_type"))
            if not kind:
                continue  # ignore photo / music sections

            rows = await t.library_media_info(section_id)
            enriched = await _enrich(t, rows, concurrency=concurrency)
            for item in enriched:
                if not item.rating_key:
                    continue
                plex_item_id = _upsert_plex_item(
                    conn, item, kind=kind, run_id=run_id, section_name=lib.get("section_name")
                )
                if item.file_size:
                    _upsert_plex_media_file(conn, plex_item_id, item, run_id=run_id)
                    files_seen += 1
                items_seen += 1

    mark_tombstones(conn, "plex_items", run_id=run_id)
    mark_tombstones(conn, "plex_media_files", run_id=run_id)

    step.items_seen = items_seen
    step.items_changed = items_seen
    return PlexSyncResult(items_seen=items_seen, files_seen=files_seen)


async def _enrich(
    client: TautulliClient,
    items: list[TautulliLibraryItem],
    *,
    concurrency: int,
) -> list[TautulliLibraryItem]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def fetch(item: TautulliLibraryItem) -> TautulliLibraryItem:
        if not item.rating_key:
            return item
        async with sem:
            try:
                meta = await client.metadata(item.rating_key)
            except UpstreamError as exc:
                logger.warning("metadata failed for rating_key=%s: %s", item.rating_key, exc)
                return item
        guid = meta.get("guid") if isinstance(meta, dict) else None
        guids = meta.get("guids") if isinstance(meta, dict) else None
        return item.model_copy(
            update={
                "guid": guid or item.guid,
                "guids": list(guids) if isinstance(guids, list) else item.guids,
            }
        )

    return list(await asyncio.gather(*(fetch(i) for i in items)))


def _upsert_plex_item(
    conn: sqlite3.Connection,
    item: TautulliLibraryItem,
    *,
    kind: str,
    run_id: int,
    section_name: str | None,
) -> int:
    ids = resolve_plex_item(item)
    return upsert(
        conn,
        "plex_items",
        {
            "rating_key": item.rating_key,
            "parent_rating_key": None,
            "grandparent_rating_key": None,
            "kind": kind,
            "title": item.title,
            "year": item.year,
            "section_id": item.section_id,
            "section_name": section_name or item.section_name,
            "tmdb_id": ids.tmdb_id,
            "tvdb_id": ids.tvdb_id,
            "imdb_id": ids.imdb_id,
            "guid": item.guid,
            "guids_json": json.dumps(item.guids) if item.guids else None,
            "season_number": None,
            "episode_number": None,
            "absolute_episode_number": None,
            "parent_title": None,
            "grandparent_title": None,
            "originally_available_at": None,
            "last_seen_sync_run_id": run_id,
            "deleted_at": None,
        },
        conflict_keys=("rating_key",),
    )


def _upsert_plex_media_file(
    conn: sqlite3.Connection,
    plex_item_id: int,
    item: TautulliLibraryItem,
    *,
    run_id: int,
) -> None:
    # library_media_info rows don't expose file_path directly. We have file_size,
    # which is what we need for size-based orphan checks; path comes via metadata
    # in a later pass if needed.
    upsert(
        conn,
        "plex_media_files",
        {
            "plex_item_id": plex_item_id,
            "rating_key": item.rating_key,
            "file_path": None,
            "size_bytes": int(item.file_size or 0),
            "container": None,
            "video_resolution": None,
            "video_codec": None,
            "last_seen_sync_run_id": run_id,
            "deleted_at": None,
        },
        conflict_keys=("plex_item_id", "rating_key"),
    )
