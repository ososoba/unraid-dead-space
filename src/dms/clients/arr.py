"""Sonarr / Radarr v3 client.

Sonarr and Radarr share the same v3 API conventions for the endpoints we use,
so one client class handles both with a `kind` discriminator.
"""

from __future__ import annotations

from typing import Any

from dms.clients.base import BaseClient
from dms.config import ArrInstance
from dms.models import ArrEpisode, ArrFile, ArrMovie, ArrSeries, ArrTag


class ArrClient(BaseClient):
    """v3 API client for Sonarr or Radarr."""

    def __init__(self, instance: ArrInstance, *, timeout: float = 30.0) -> None:
        super().__init__(
            instance.url,
            timeout=timeout,
            default_headers={"X-Api-Key": instance.api_key},
        )
        self.instance = instance

    async def system_status(self) -> dict[str, Any]:
        return await self.get_json("/api/v3/system/status")

    async def list_tags(self) -> list[ArrTag]:
        rows = await self.get_json("/api/v3/tag")
        return [ArrTag.model_validate(r) for r in rows]

    # ----- Radarr -----
    async def list_movies(self) -> list[ArrMovie]:
        if self.instance.kind != "radarr":
            raise ValueError(f"list_movies called on {self.instance.kind!r}")
        rows = await self.get_json("/api/v3/movie")
        return [ArrMovie.model_validate(r) for r in rows]

    # ----- Sonarr -----
    async def list_series(self) -> list[ArrSeries]:
        if self.instance.kind != "sonarr":
            raise ValueError(f"list_series called on {self.instance.kind!r}")
        rows = await self.get_json("/api/v3/series", params={"includeSeasonImages": "false"})
        return [ArrSeries.model_validate(r) for r in rows]

    async def list_episodes(self, series_id: int) -> list[ArrEpisode]:
        rows = await self.get_json("/api/v3/episode", params={"seriesId": series_id})
        return [ArrEpisode.model_validate(r) for r in rows]

    async def list_episode_files(self, series_id: int) -> list[ArrFile]:
        rows = await self.get_json("/api/v3/episodefile", params={"seriesId": series_id})
        return [ArrFile.model_validate(r) for r in rows]
