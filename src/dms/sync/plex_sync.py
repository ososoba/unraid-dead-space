"""Plex inventory sync (via Tautulli).

Pulls library_media_info per non-empty section, enriches each item with
guids + media_info via get_metadata, and upserts into plex_items +
plex_media_files. media_info gives us file_path / container / resolution /
codec — needed for the file-level orphan check (PLAN.md decision #19).

On the first sync this triggers a Tautulli library refresh (refresh=true)
because the cached media_info table is otherwise stale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

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


@dataclass
class PlexFileMeta:
    """One row destined for plex_media_files."""

    file_path: str | None
    size_bytes: int
    container: str | None
    video_resolution: str | None
    video_codec: str | None


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
            enriched, file_meta = await _enrich_with_metadata(t, rows, concurrency=concurrency)
            for item in enriched:
                if not item.rating_key:
                    continue
                plex_item_id = _upsert_plex_item(
                    conn,
                    item,
                    kind=kind,
                    run_id=run_id,
                    section_name=lib.get("section_name"),
                )
                # Each plex_item may have multiple files (multi-part movies);
                # we upsert one row per (plex_item_id, rating_key + index slot).
                # For v1 the get_library_media_info row only ever yields a single
                # rating_key per plex_item, so we collapse parts under the same
                # rating_key — practical for movies/shows where Plex sets a
                # single primary file per item.
                files = file_meta.get(item.rating_key, [])
                if files:
                    for fmeta in files:
                        _upsert_plex_media_file(
                            conn,
                            plex_item_id,
                            item.rating_key,
                            fmeta,
                            run_id=run_id,
                        )
                        files_seen += 1
                elif item.file_size:
                    # Fallback when metadata didn't return any parts.
                    _upsert_plex_media_file(
                        conn,
                        plex_item_id,
                        item.rating_key,
                        PlexFileMeta(
                            file_path=None,
                            size_bytes=int(item.file_size or 0),
                            container=None,
                            video_resolution=None,
                            video_codec=None,
                        ),
                        run_id=run_id,
                    )
                    files_seen += 1
                items_seen += 1

    mark_tombstones(conn, "plex_items", run_id=run_id)
    mark_tombstones(conn, "plex_media_files", run_id=run_id)

    step.items_seen = items_seen
    step.items_changed = items_seen
    return PlexSyncResult(items_seen=items_seen, files_seen=files_seen)


async def _enrich_with_metadata(
    client: TautulliClient,
    items: list[TautulliLibraryItem],
    *,
    concurrency: int,
) -> tuple[list[TautulliLibraryItem], dict[int, list[PlexFileMeta]]]:
    """Fan out get_metadata; return (items_with_guids, files_by_rating_key)."""
    sem = asyncio.Semaphore(max(1, concurrency))
    file_meta: dict[int, list[PlexFileMeta]] = {}

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
        files = _extract_media_files(meta if isinstance(meta, dict) else {})
        if files:
            file_meta[item.rating_key] = files
        return item.model_copy(
            update={
                "guid": guid or item.guid,
                "guids": list(guids) if isinstance(guids, list) else item.guids,
            }
        )

    enriched = list(await asyncio.gather(*(fetch(i) for i in items)))
    return enriched, file_meta


def _extract_media_files(meta: dict[str, Any]) -> list[PlexFileMeta]:
    """Pull file rows out of Tautulli's get_metadata response.

    Shape (per Tautulli wiki):
      meta['media_info']: list of media bundles (one per Plex Media row)
      each item has 'parts': list of files with 'file', 'container',
      'file_size', 'video_resolution', 'video_codec'.
    """
    out: list[PlexFileMeta] = []
    media_info = meta.get("media_info") or []
    if not isinstance(media_info, list):
        return out
    for media in media_info:
        if not isinstance(media, dict):
            continue
        # Some shape variants store these at the media level instead of part level.
        media_resolution = media.get("video_resolution")
        media_codec = media.get("video_codec")
        media_container = media.get("container")
        for part in media.get("parts") or []:
            if not isinstance(part, dict):
                continue
            try:
                size = int(part.get("file_size") or 0)
            except (TypeError, ValueError):
                size = 0
            out.append(
                PlexFileMeta(
                    file_path=str(part.get("file") or "") or None,
                    size_bytes=size,
                    container=str(part.get("container") or media_container or "") or None,
                    video_resolution=(
                        str(part.get("video_resolution") or media_resolution or "") or None
                    ),
                    video_codec=(str(part.get("video_codec") or media_codec or "") or None),
                )
            )
    return out


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
    rating_key: int,
    fmeta: PlexFileMeta,
    *,
    run_id: int,
) -> None:
    upsert(
        conn,
        "plex_media_files",
        {
            "plex_item_id": plex_item_id,
            "rating_key": rating_key,
            "file_path": fmeta.file_path,
            "size_bytes": fmeta.size_bytes,
            "container": fmeta.container,
            "video_resolution": fmeta.video_resolution,
            "video_codec": fmeta.video_codec,
            "last_seen_sync_run_id": run_id,
            "deleted_at": None,
        },
        conflict_keys=("plex_item_id", "rating_key"),
    )
