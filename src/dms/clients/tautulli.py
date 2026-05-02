"""Tautulli client.

Tautulli has a single endpoint (`/api/v2`) with a `cmd=` query param. Wraps
that into typed methods. History pagination uses `start` + `length`. Library
inventory comes from `get_libraries_table` + `get_library_media_info` per
section.
"""

from __future__ import annotations

from typing import Any

from dms.clients.base import BaseClient, UpstreamError
from dms.config import TautulliConfig
from dms.models import TautulliHistoryRow, TautulliLibraryItem, TautulliUser


class TautulliClient(BaseClient):
    def __init__(self, config: TautulliConfig, *, timeout: float = 30.0) -> None:
        super().__init__(config.url, timeout=timeout)
        self._api_key = config.api_key

    async def _cmd(self, cmd: str, **params: Any) -> Any:
        """Call /api/v2?cmd=<cmd>; return response['response']['data']."""
        full = {"apikey": self._api_key, "cmd": cmd, **params}
        payload = await self.get_json("/api/v2", params=full)
        wrapper = payload.get("response", {}) if isinstance(payload, dict) else {}
        if wrapper.get("result") != "success":
            raise UpstreamError(f"Tautulli {cmd} failed: {wrapper.get('message')!r}")
        return wrapper.get("data")

    async def server_info(self) -> dict[str, Any]:
        return await self._cmd("get_server_info") or {}

    async def list_users(self) -> list[TautulliUser]:
        rows = await self._cmd("get_users") or []
        return [TautulliUser.model_validate(r) for r in rows]

    async def list_libraries(self) -> list[dict[str, Any]]:
        """Return the libraries table; each row has section_id/section_name/section_type."""
        data = await self._cmd("get_libraries_table") or {}
        rows = data.get("data", []) if isinstance(data, dict) else data
        return list(rows)

    async def library_media_info(
        self, section_id: int, *, length: int = 1000
    ) -> list[TautulliLibraryItem]:
        """Page through one library section's media."""
        items: list[TautulliLibraryItem] = []
        start = 0
        while True:
            data = (
                await self._cmd(
                    "get_library_media_info",
                    section_id=section_id,
                    start=start,
                    length=length,
                )
                or {}
            )
            rows = data.get("data", []) if isinstance(data, dict) else []
            if not rows:
                break
            items.extend(TautulliLibraryItem.model_validate(r) for r in rows)
            if len(rows) < length:
                break
            start += length
        return items

    async def history(
        self, *, length: int = 500, after_row_id: int | None = None
    ) -> list[TautulliHistoryRow]:
        """Page through history. `after_row_id` for incremental sync."""
        rows: list[TautulliHistoryRow] = []
        start = 0
        while True:
            data = await self._cmd("get_history", start=start, length=length) or {}
            page = data.get("data", []) if isinstance(data, dict) else []
            if not page:
                break
            for raw in page:
                row = TautulliHistoryRow.model_validate(raw)
                if after_row_id is not None and row.id <= after_row_id:
                    return rows
                rows.append(row)
            if len(page) < length:
                break
            start += length
        return rows

    async def metadata(self, rating_key: int) -> dict[str, Any]:
        """Fetch metadata for one rating_key — has guids array with tmdb/tvdb/imdb."""
        return await self._cmd("get_metadata", rating_key=rating_key) or {}
