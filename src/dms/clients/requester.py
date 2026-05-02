"""Overseerr / Seerr / Jellyseerr request-service adapter.

All three share the same `/api/v1/request` shape and `X-Api-Key` header.
A `flavor` field lets future divergence surface cleanly.
"""

from __future__ import annotations

from typing import Any

from dms.clients.base import BaseClient
from dms.config import RequesterConfig
from dms.models import RequesterUser, RequestRecord


class RequesterClient(BaseClient):
    def __init__(self, config: RequesterConfig, *, timeout: float = 30.0) -> None:
        super().__init__(
            config.url,
            timeout=timeout,
            default_headers={"X-Api-Key": config.api_key},
        )
        self.flavor = config.source

    async def status(self) -> dict[str, Any]:
        return await self.get_json("/api/v1/status")

    async def list_users(self) -> list[RequesterUser]:
        # /api/v1/user is paginated: { pageInfo, results }
        users: list[RequesterUser] = []
        skip = 0
        take = 100
        while True:
            payload = await self.get_json("/api/v1/user", params={"skip": skip, "take": take})
            results = payload.get("results", []) if isinstance(payload, dict) else []
            if not results:
                break
            users.extend(RequesterUser.model_validate(r) for r in results)
            page = payload.get("pageInfo", {}) if isinstance(payload, dict) else {}
            total = int(page.get("results", len(users)))
            if len(users) >= total or len(results) < take:
                break
            skip += take
        return users

    async def list_requests(self) -> list[RequestRecord]:
        # /api/v1/request paginated; filter=all gets every state
        records: list[RequestRecord] = []
        skip = 0
        take = 100
        while True:
            payload = await self.get_json(
                "/api/v1/request",
                params={"take": take, "skip": skip, "filter": "all", "sort": "added"},
            )
            results = payload.get("results", []) if isinstance(payload, dict) else []
            if not results:
                break
            records.extend(RequestRecord.model_validate(r) for r in results)
            page = payload.get("pageInfo", {}) if isinstance(payload, dict) else {}
            total = int(page.get("results", len(records)))
            if len(records) >= total or len(results) < take:
                break
            skip += take
        return records
