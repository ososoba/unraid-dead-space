"""Spike-internal models.

Loose Pydantic models — only fields the spike actually consumes are typed.
Upstream APIs include far more; we use `model_config = ConfigDict(extra="ignore")`
so extra fields don't fail validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


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
    id: int = Field(alias="id")  # row_id; stable PK
    rating_key: int | None = None
    parent_rating_key: int | None = None
    grandparent_rating_key: int | None = None
    media_type: str | None = None  # movie | episode
    user_id: int | None = None
    user: str | None = None
    title: str | None = None
    parent_title: str | None = None
    grandparent_title: str | None = None
    date: int | None = None  # unix
    started: int | None = None
    stopped: int | None = None
    percent_complete: int | None = None
    watched_status: float | None = None  # 0 | 0.5 | 1


class TautulliLibraryItem(_Loose):
    rating_key: int
    section_id: int | None = None
    section_name: str | None = None
    title: str | None = None
    year: int | None = None
    media_type: str | None = None  # movie | show | season | episode
    guid: str | None = None
    guids: list[str] = Field(default_factory=list)
    file_size: int | None = None


class TautulliUser(_Loose):
    user_id: int
    username: str | None = None
    friendly_name: str | None = None


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
