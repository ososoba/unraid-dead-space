"""Shared async HTTP base.

Wraps httpx with timeout, retry-on-transient-error, and a sanitized
__repr__ so API keys never leak into logs or tracebacks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Status codes worth retrying. 401/403/404 are NOT retried — they're config bugs.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class UpstreamError(Exception):
    """Base class for upstream API failures."""


class UpstreamHTTPError(UpstreamError):
    def __init__(self, status: int, url: str, body_snippet: str = "") -> None:
        super().__init__(f"HTTP {status} from {url}: {body_snippet[:200]}")
        self.status = status
        self.url = url


class UpstreamTimeoutError(UpstreamError):
    pass


class BaseClient:
    """Tiny wrapper around httpx.AsyncClient with retry + sanitized repr."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        default_headers: dict[str, str] | None = None,
        max_retries: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers=default_headers or {},
        )

    def __repr__(self) -> str:  # secrets-safe
        return f"{type(self).__name__}(base_url={self._base_url!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BaseClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Issue request, retry transient failures, return parsed JSON."""
        backoff = 1.0
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.request(method, path, params=params, json=json)
            except httpx.TimeoutException as exc:
                last_exc = UpstreamTimeoutError(f"timeout {method} {path}")
                logger.warning("timeout %s %s (attempt %d): %s", method, path, attempt, exc)
            except httpx.HTTPError as exc:
                last_exc = UpstreamError(f"transport error {method} {path}: {exc!s}")
                logger.warning("transport error %s %s (attempt %d)", method, path, attempt)
            else:
                if response.status_code in _RETRY_STATUSES:
                    last_exc = UpstreamHTTPError(
                        response.status_code,
                        str(response.request.url),
                        response.text,
                    )
                    logger.warning(
                        "retryable HTTP %d on %s %s (attempt %d)",
                        response.status_code,
                        method,
                        path,
                        attempt,
                    )
                elif response.is_error:
                    raise UpstreamHTTPError(
                        response.status_code,
                        str(response.request.url),
                        response.text,
                    )
                else:
                    return response.json()

            if attempt < self._max_retries:
                await asyncio.sleep(backoff)
                backoff *= 2

        assert last_exc is not None
        raise last_exc

    async def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self.request_json("GET", path, params=params)
