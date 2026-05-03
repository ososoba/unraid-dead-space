"""Spike-internal models.

Loose Pydantic models — only fields the spike actually consumes are typed.
Upstream APIs include far more; we use `model_config = ConfigDict(extra="ignore")`
so extra fields don't fail validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


def _empty_to_none(v: Any) -> Any:
    """Tautulli returns '' for missing optional ints (file_size, year, etc).

    Pydantic v2 won't coerce that — apply via BeforeValidator on every
    optional-int field that might come back empty.
    """
    if v == "" or v == "null":
        return None
    return v


OptInt = Annotated[int | None, BeforeValidator(_empty_to_none)]
OptStr = Annotated[str | None, BeforeValidator(_empty_to_none)]


# ---------- Arr ----------


class ArrTag(_Loose):
    id: int
    label: str


class ArrFile(_Loose):
    """Sonarr episodeFile or Radarr movieFile (shared shape)."""

    id: int
    path: str | None = None
    size: int = 0
    date_added: datetime | None = Field(default=None, alias="dateAdded")
    quality: dict | None = None  # opaque; not parsed in spike


class ArrMovie(_Loose):
    id: int
    title: str
    year: int | None = None
    tmdb_id: int | None = Field(default=None, alias="tmdbId")
    imdb_id: str | None = Field(default=None, alias="imdbId")
    monitored: bool = False
    added: datetime | None = None
    tags: list[int] = Field(default_factory=list)
    size_on_disk: int = Field(default=0, alias="sizeOnDisk")
    has_file: bool = Field(default=False, alias="hasFile")
    movie_file: ArrFile | None = Field(default=None, alias="movieFile")


class ArrEpisode(_Loose):
    id: int
    series_id: int = Field(alias="seriesId")
    season_number: int = Field(alias="seasonNumber")
    episode_number: int = Field(alias="episodeNumber")
    absolute_episode_number: int | None = Field(default=None, alias="absoluteEpisodeNumber")
    title: str = ""
    air_date_utc: datetime | None = Field(default=None, alias="airDateUtc")
    monitored: bool = False
    has_file: bool = Field(default=False, alias="hasFile")
    episode_file_id: int = Field(default=0, alias="episodeFileId")


class ArrSeries(_Loose):
    id: int
    title: str
    year: int | None = None
    tvdb_id: int | None = Field(default=None, alias="tvdbId")
    tmdb_id: int | None = Field(default=None, alias="tmdbId")
    imdb_id: str | None = Field(default=None, alias="imdbId")
    monitored: bool = False
    added: datetime | None = None
    tags: list[int] = Field(default_factory=list)
    statistics: dict | None = None  # contains sizeOnDisk, episodeCount, etc.

    @property
    def size_on_disk(self) -> int:
        if self.statistics:
            return int(self.statistics.get("sizeOnDisk", 0) or 0)
        return 0

    @property
    def episode_count(self) -> int:
        if self.statistics:
            return int(self.statistics.get("episodeCount", 0) or 0)
        return 0


# ---------- Tautulli ----------


class TautulliHistoryRow(_Loose):
    # Tautulli's history row_id; the stable PK we cursor on. Some installs
    # return empty strings for `id` on certain rows (observed live). Allow
    # None at the parsing layer; the sync skips rows without an id since
    # they can't be inserted (UNIQUE NOT NULL on watch_events.source_row_id).
    id: OptInt = Field(default=None, alias="id")
    rating_key: OptInt = None
    parent_rating_key: OptInt = None
    grandparent_rating_key: OptInt = None
    media_type: OptStr = None  # movie | episode
    user_id: OptInt = None
    user: OptStr = None
    title: OptStr = None
    parent_title: OptStr = None
    grandparent_title: OptStr = None
    date: OptInt = None  # unix
    started: OptInt = None
    stopped: OptInt = None
    percent_complete: OptInt = None
    watched_status: float | None = None  # 0 | 0.5 | 1


class TautulliLibraryItem(_Loose):
    rating_key: OptInt = None
    section_id: OptInt = None
    section_name: OptStr = None
    title: OptStr = None
    year: OptInt = None
    media_type: OptStr = None  # movie | show | season | episode
    guid: OptStr = None
    guids: list[str] = Field(default_factory=list)
    file_size: OptInt = None


class TautulliUser(_Loose):
    user_id: int
    username: OptStr = None
    friendly_name: OptStr = None


# ---------- Overseerr / Seerr ----------


class RequesterUser(_Loose):
    id: int
    plex_username: str | None = Field(default=None, alias="plexUsername")
    jellyfin_username: str | None = Field(default=None, alias="jellyfinUsername")
    username: str | None = None
    display_name: str | None = Field(default=None, alias="displayName")


class RequestMedia(_Loose):
    media_type: Literal["movie", "tv"] | None = Field(default=None, alias="mediaType")
    tmdb_id: int | None = Field(default=None, alias="tmdbId")
    tvdb_id: int | None = Field(default=None, alias="tvdbId")
    imdb_id: str | None = Field(default=None, alias="imdbId")


class RequestRecord(_Loose):
    id: int
    status: int | None = None
    created_at: datetime | None = Field(default=None, alias="createdAt")
    requested_by: RequesterUser | None = Field(default=None, alias="requestedBy")
    media: RequestMedia | None = None
