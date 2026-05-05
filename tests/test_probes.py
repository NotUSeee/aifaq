from __future__ import annotations

import httpx
import pytest

from status_service.probes.http import derive_db_redis, probe_health, probe_readiness
from status_service.probes.proxy import PROXY_INTERNAL_SERVICES, probe_status_api
from status_service.probes import ProbeResult


@pytest.mark.asyncio
async def test_probe_health_classifies_200_under_threshold_as_operational(respx_mock):
    respx_mock.get("https://x/health").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with httpx.AsyncClient() as client:
        result = await probe_health(client, "https://x")
    assert result.status == "operational"
    assert result.http_status == 200


@pytest.mark.asyncio
async def test_probe_health_classifies_500_as_down(respx_mock):
    respx_mock.get("https://x/health").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        result = await probe_health(client, "https://x")
    assert result.status == "down"


@pytest.mark.asyncio
async def test_probe_health_timeout_returns_down(respx_mock):
    respx_mock.get("https://x/health").mock(side_effect=httpx.TimeoutException("slow"))
    async with httpx.AsyncClient() as client:
        result = await probe_health(client, "https://x")
    assert result.status == "down"
    assert result.error == "timeout"


@pytest.mark.asyncio
async def test_probe_readiness_parses_db_redis(respx_mock):
    respx_mock.get("https://x/readiness").mock(
        return_value=httpx.Response(200, json={"ok": True, "db": "ok", "redis": "ok"})
    )
    async with httpx.AsyncClient() as client:
        parent, body = await probe_readiness(client, "https://x")
    rows = derive_db_redis(parent, body)
    by_name = {r.service_name: r for r in rows}
    assert by_name["Database"].status == "operational"
    assert by_name["Cache"].status == "operational"


@pytest.mark.asyncio
async def test_derive_db_redis_marks_down_when_parent_down():
    """When the public site is unreachable we report Database and Cache as
    `down` to match user-perceived availability (a visitor can't reach
    them regardless of whether they're internally healthy)."""
    parent = ProbeResult(service_name="Public Site", status="down", source="external")
    rows = derive_db_redis(parent, {})
    by_name = {r.service_name: r for r in rows}
    assert by_name["Database"].status == "down"
    assert by_name["Cache"].status == "down"


@pytest.mark.asyncio
async def test_proxy_status_api_emits_one_result_per_service(respx_mock):
    respx_mock.get("https://x/status/api").mock(
        return_value=httpx.Response(200, json={
            "current": [
                {"name": "Bot", "status": "operational", "response_ms": 12},
                {"name": "Plugin Runner", "status": "degraded", "response_ms": 2400},
                {"name": "Bot Worker", "status": "down", "response_ms": None},
            ],
        })
    )
    async with httpx.AsyncClient() as client:
        results, body = await probe_status_api(client, "https://x")
    assert len(results) == 3
    by = {r.service_name: r for r in results}
    assert by["Bot"].status == "operational"
    assert by["Plugin Runner"].status == "degraded"
    assert by["Bot Worker"].status == "down"
    assert body is not None


@pytest.mark.asyncio
async def test_proxy_timeout_marks_each_internal_service_unknown(respx_mock):
    """Regression: when /status/api times out we used to write a single
    `__proxy_failed__` sentinel and leave per-service rows stale at their
    last `operational` value for 5 minutes. Now every internal service
    must get a fresh `unknown` row this same cycle."""
    respx_mock.get("https://x/status/api").mock(side_effect=httpx.TimeoutException("slow"))
    async with httpx.AsyncClient() as client:
        results, body = await probe_status_api(client, "https://x")
    assert body is None
    names = {r.service_name for r in results}
    assert names == set(PROXY_INTERNAL_SERVICES)
    for r in results:
        assert r.status == "unknown"
        assert r.source == "proxy"
        assert r.error == "timeout"


@pytest.mark.asyncio
async def test_proxy_5xx_marks_each_internal_service_unknown(respx_mock):
    respx_mock.get("https://x/status/api").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        results, body = await probe_status_api(client, "https://x")
    assert body is None
    assert {r.service_name for r in results} == set(PROXY_INTERNAL_SERVICES)
    assert all(r.status == "unknown" for r in results)
