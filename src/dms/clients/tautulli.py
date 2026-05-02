"""Tautulli client.

Tautulli has a single endpoint (`/api/v2`) with a `cmd=` query param. Wraps
that into typed methods. History pagination uses `start` + `length`. Library
inventory comes from `get_libraries_table` + `get_library_media_info` per
section.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
        self, section_id: int, *, length: int = 1000, refresh: bool = True
    ) -> list[TautulliLibraryItem]:
        """Page through one library section's media.

        Tautulli caches `get_library_media_info` separately from Plex itself.
        Without `refresh=true` on the first request, the cache may report
        many fewer items than Plex actually has (we observed 354 vs 1048
        on a real install). Default to refreshing on the first page; pass
        `refresh=False` if you've just refreshed and want to repaginate.
        """
        items: list[TautulliLibraryItem] = []
        start = 0
        first = True
        while True:
            params: dict[str, Any] = {
                "section_id": section_id,
                "start": start,
                "length": length,
            }
            if refresh and first:
                params["refresh"] = "true"
            data = await self._cmd("get_library_media_info", **params) or {}
            first = False
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
        """Convenience: drain `iter_history` into a list. Avoid for large pulls;
        use `iter_history` so each page can be persisted before the next request."""
        rows: list[TautulliHistoryRow] = []
        async for page in self.iter_history(length=length, after_row_id=after_row_id):
            rows.extend(page)
        return rows

    async def iter_history(
        self,
        *,
        length: int = 500,
        after_row_id: int | None = None,
        not_before_unix: int | None = None,
    ) -> AsyncIterator[list[TautulliHistoryRow]]:
        """Yield `get_history` page-by-page so the caller can persist + checkpoint
        between pages. Stops when:
          - the page is empty / short (end of history),
          - `after_row_id` is set and an older row appears (incremental cutoff),
          - `not_before_unix` is set and a row's `date` is earlier (retention cap).
        """
        start = 0
        while True:
            data = await self._cmd("get_history", start=start, length=length) or {}
            page_raw = data.get("data", []) if isinstance(data, dict) else []
            if not page_raw:
                return
            page: list[TautulliHistoryRow] = []
            stop = False
            for raw in page_raw:
                row = TautulliHistoryRow.model_validate(raw)
                if after_row_id is not None and row.id is not None and row.id <= after_row_id:
                    stop = True
                    break
                if (
                    not_before_unix is not None
                    and row.date is not None
                    and row.date < not_before_unix
                ):
                    stop = True
                    break
                page.append(row)
            if page:
                yield page
            if stop or len(page_raw) < length:
                return
            start += length

    async def metadata(self, rating_key: int) -> dict[str, Any]:
        """Fetch metadata for one rating_key — has guids array with tmdb/tvdb/imdb."""
        return await self._cmd("get_metadata", rating_key=rating_key) or {}
