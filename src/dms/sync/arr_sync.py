"""Arr (Sonarr/Radarr) → DB sync.

Pulls items + episodes (Sonarr) + files (Sonarr+Radarr) + tags from one
Arr instance and upserts into:
- instances     (one row per configured instance)
- arr_items
- arr_episodes  (Sonarr only)
- arr_files     (movie OR episode kind)
- tags

Tombstones rows missed by this run, scoped to the instance so a failure
in another instance does not delete this one's items.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from dms.clients.arr import ArrClient
from dms.config import ArrInstance
from dms.models import ArrEpisode, ArrFile, ArrMovie, ArrSeries
from dms.sync.runs import RunStep
from dms.sync.upsert import mark_tombstones, upsert
from dms.tag_parser import ParsedTag

logger = logging.getLogger(__name__)


@dataclass
class ArrSyncResult:
    instance_id: int
    items_seen: int
    items_changed: int
    files_seen: int
    episodes_seen: int


def _iso(dt: object) -> str | None:
    if dt is None:
        return None
    try:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)  # type: ignore[no-any-return]
    except Exception:
        return None


def _upsert_instance(conn: sqlite3.Connection, instance: ArrInstance) -> int:
    return upsert(
        conn,
        "instances",
        {
            "kind": instance.kind,
            "slug": instance.slug,
            "name": instance.name,
            "url": instance.url,
            "api_key": instance.api_key,
            "enabled": 1,
            "last_seen_ok_at": None,
            "last_error": None,
            "updated_at": _iso(__import__("datetime").datetime.now()),
        },
        conflict_keys=("slug",),
        update_columns=["kind", "name", "url", "api_key", "enabled", "updated_at"],
    )


async def sync_arr_instance(
    conn: sqlite3.Connection,
    instance: ArrInstance,
    *,
    run_id: int,
    step: RunStep,
    timeout: float = 30.0,
) -> ArrSyncResult:
    """Pull one Arr instance and persist. Caller wraps in `step()` context."""
    instance_id = _upsert_instance(conn, instance)
    items_seen = 0
    files_seen = 0
    episodes_seen = 0

    async with ArrClient(instance, timeout=timeout) as client:
        tags = {t.id: t.label for t in await client.list_tags()}

        if instance.kind == "radarr":
            for movie in await client.list_movies():
                arr_item_id = _upsert_movie(conn, instance_id, movie, run_id)
                _upsert_movie_tags(conn, instance_id, arr_item_id, movie.tags, tags)
                if movie.movie_file is not None and movie.movie_file.id:
                    _upsert_movie_file(conn, instance_id, arr_item_id, movie, run_id)
                    files_seen += 1
                items_seen += 1
        else:  # sonarr
            for series in await client.list_series():
                arr_item_id = _upsert_series(conn, instance_id, series, run_id)
                _upsert_series_tags(conn, instance_id, arr_item_id, series.tags, tags)
                items_seen += 1
                episodes = await client.list_episodes(series.id)
                ep_id_map = _upsert_episodes(conn, instance_id, arr_item_id, episodes, run_id)
                episodes_seen += len(episodes)
                for f in await client.list_episode_files(series.id):
                    _upsert_episode_file(conn, instance_id, arr_item_id, f, ep_id_map, run_id)
                    files_seen += 1

    # Tombstones, scoped to this instance only.
    mark_tombstones(
        conn,
        "arr_items",
        run_id=run_id,
        scope_clause="instance_id = ?",
        scope_params=(instance_id,),
    )
    mark_tombstones(
        conn,
        "arr_episodes",
        run_id=run_id,
        scope_clause="instance_id = ?",
        scope_params=(instance_id,),
    )
    mark_tombstones(
        conn,
        "arr_files",
        run_id=run_id,
        scope_clause="instance_id = ?",
        scope_params=(instance_id,),
    )

    # last_seen_ok_at on the instance row.
    with conn:
        conn.execute(
            "UPDATE instances SET last_seen_ok_at = datetime('now'), last_error = NULL "
            "WHERE id = ?",
            (instance_id,),
        )

    step.items_seen = items_seen
    step.items_changed = items_seen  # upserts are always "changed" from our POV

    return ArrSyncResult(
        instance_id=instance_id,
        items_seen=items_seen,
        items_changed=items_seen,
        files_seen=files_seen,
        episodes_seen=episodes_seen,
    )


# ---------- Helpers (movies) ----------


def _upsert_movie(conn: sqlite3.Connection, instance_id: int, m: ArrMovie, run_id: int) -> int:
    return upsert(
        conn,
        "arr_items",
        {
            "instance_id": instance_id,
            "kind": "movie",
            "arr_id": m.id,
            "title": m.title,
            "year": m.year,
            "tmdb_id": m.tmdb_id,
            "tvdb_id": None,
            "imdb_id": m.imdb_id,
            "monitored": int(bool(m.monitored)),
            "added_at": _iso(m.added),
            "last_seen_sync_run_id": run_id,
            "deleted_at": None,
        },
        conflict_keys=("instance_id", "arr_id"),
    )


def _upsert_movie_file(
    conn: sqlite3.Connection,
    instance_id: int,
    arr_item_id: int,
    movie: ArrMovie,
    run_id: int,
) -> None:
    f = movie.movie_file
    if f is None:
        return
    upsert(
        conn,
        "arr_files",
        {
            "instance_id": instance_id,
            "arr_item_id": arr_item_id,
            "kind": "movie",
            "arr_file_id": f.id,
            "arr_episode_id": None,
            "season_number": None,
            "episode_number": None,
            "path": f.path,
            "size_bytes": int(f.size or 0),
            "date_added": _iso(f.date_added),
            "quality": None,
            "last_seen_sync_run_id": run_id,
            "deleted_at": None,
        },
        conflict_keys=("instance_id", "kind", "arr_file_id"),
    )


def _upsert_movie_tags(
    conn: sqlite3.Connection,
    instance_id: int,
    arr_item_id: int,
    tag_ids: list[int],
    label_by_id: dict[int, str],
) -> None:
    # Replace strategy: simplest correct option for tags. Drop and re-insert.
    conn.execute(
        "DELETE FROM tags WHERE instance_id = ? AND arr_item_id = ?",
        (instance_id, arr_item_id),
    )
    for tid in tag_ids:
        label = label_by_id.get(tid)
        if not label:
            continue
        parsed = ParsedTag.parse(label)
        conn.execute(
            "INSERT INTO tags "
            "(instance_id, arr_item_id, raw_tag, parsed_requester_id, "
            " parsed_requester_name, is_unparseable) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                instance_id,
                arr_item_id,
                label,
                parsed.requester_id,
                parsed.requester_name,
                int(parsed.is_unparseable),
            ),
        )


# ---------- Helpers (series + episodes) ----------


def _upsert_series(conn: sqlite3.Connection, instance_id: int, s: ArrSeries, run_id: int) -> int:
    return upsert(
        conn,
        "arr_items",
        {
            "instance_id": instance_id,
            "kind": "series",
            "arr_id": s.id,
            "title": s.title,
            "year": s.year,
            "tmdb_id": s.tmdb_id,
            "tvdb_id": s.tvdb_id,
            "imdb_id": s.imdb_id,
            "monitored": int(bool(s.monitored)),
            "added_at": _iso(s.added),
            "last_seen_sync_run_id": run_id,
            "deleted_at": None,
        },
        conflict_keys=("instance_id", "arr_id"),
    )


def _upsert_series_tags(
    conn: sqlite3.Connection,
    instance_id: int,
    arr_item_id: int,
    tag_ids: list[int],
    label_by_id: dict[int, str],
) -> None:
    _upsert_movie_tags(conn, instance_id, arr_item_id, tag_ids, label_by_id)


def _upsert_episodes(
    conn: sqlite3.Connection,
    instance_id: int,
    arr_item_id: int,
    episodes: list[ArrEpisode],
    run_id: int,
) -> dict[int, int]:
    """Upsert episodes, return mapping arr_episode_id → arr_episodes.id."""
    id_map: dict[int, int] = {}
    for ep in episodes:
        local_id = upsert(
            conn,
            "arr_episodes",
            {
                "instance_id": instance_id,
                "arr_item_id": arr_item_id,
                "arr_episode_id": ep.id,
                "season_number": ep.season_number,
                "episode_number": ep.episode_number,
                "absolute_episode_number": ep.absolute_episode_number,
                "title": ep.title,
                "air_date": _iso(ep.air_date_utc),
                "monitored": int(bool(ep.monitored)),
                "has_file": int(bool(ep.has_file)),
                "arr_episode_file_id": ep.episode_file_id or None,
                "is_special": int(ep.season_number == 0),
                "last_seen_sync_run_id": run_id,
                "deleted_at": None,
            },
            conflict_keys=("instance_id", "arr_episode_id"),
        )
        id_map[ep.id] = local_id
    return id_map


def _upsert_episode_file(
    conn: sqlite3.Connection,
    instance_id: int,
    arr_item_id: int,
    f: ArrFile,
    ep_id_map: dict[int, int],
    run_id: int,
) -> None:
    # episodeFile may be referenced by multiple episodes (rare but possible);
    # we don't have the episode link in ArrFile itself, so leave arr_episode_id NULL.
    upsert(
        conn,
        "arr_files",
        {
            "instance_id": instance_id,
            "arr_item_id": arr_item_id,
            "kind": "episode",
            "arr_file_id": f.id,
            "arr_episode_id": None,
            "season_number": None,
            "episode_number": None,
            "path": f.path,
            "size_bytes": int(f.size or 0),
            "date_added": _iso(f.date_added),
            "quality": None,
            "last_seen_sync_run_id": run_id,
            "deleted_at": None,
        },
        conflict_keys=("instance_id", "kind", "arr_file_id"),
    )
