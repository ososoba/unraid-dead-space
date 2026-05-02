"""Base HTTP client: retry behavior + secret-safe repr."""

from __future__ import annotations

import httpx
import pytest
import respx

from dms.clients.base import BaseClient, UpstreamHTTPError


@pytest.mark.asyncio
@respx.mock
async def test_returns_json_on_success() -> None:
    respx.get("http://api.test/ok").mock(return_value=httpx.Response(200, json={"hello": "world"}))
    async with BaseClient("http://api.test", timeout=1.0) as client:
        result = await client.get_json("/ok")
    assert result == {"hello": "world"}


@pytest.mark.asyncio
@respx.mock
async def test_retries_on_503_then_succeeds() -> None:
    route = respx.get("http://api.test/flaky").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    async with BaseClient("http://api.test", timeout=1.0, max_retries=3) as client:
        result = await client.get_json("/flaky")
    assert result == {"ok": True}
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_does_not_retry_on_401() -> None:
    route = respx.get("http://api.test/auth").mock(return_value=httpx.Response(401))
    async with BaseClient("http://api.test", timeout=1.0, max_retries=3) as client:
        with pytest.raises(UpstreamHTTPError) as info:
            await client.get_json("/auth")
    assert info.value.status == 401
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_repr_does_not_leak_api_key() -> None:
    client = BaseClient(
        "http://api.test",
        default_headers={"X-Api-Key": "super-secret-12345"},
    )
    representation = repr(client)
    assert "super-secret" not in representation
    assert "api.test" in representation
    await client.aclose()
