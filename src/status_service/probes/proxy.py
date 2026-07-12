from __future__ import annotations

import time
from typing import Any

import httpx

from . import USER_AGENT, ProbeResult

# Internal-tier services we expect to read from the platform's /status/api.
# Database/Cache are derived separately via /readiness, so they're not here.
PROXY_INTERNAL_SERVICES = [
    "Dashboard", "Gateway", "Plugin Runner", "Orchestrator",
    "Bot", "Bot Worker", "Analytics", "Sandbox",
]

# Services the platform only reports when configured (see the platform's
# api_status._build_service_order). They join the down/unknown fan-out only
# after they've actually been observed, so a stack without them never grows
# phantom components during an outage.
OPTIONAL_PROXY_SERVICES = [
    "Image Service", "WebSocket Broker",
]


async def probe_status_api(
    client: httpx.AsyncClient,
    base_url: str,
    expected_services: list[str] | None = None,
) -> tuple[list[ProbeResult], dict[str, Any] | None]:
    """Proxy through to the platform's /status/api. Returns one ProbeResult
    per internal service (Bot, Bot Worker, Plugin Runner, etc.) and the
    raw response body so the caller can extract additional metadata.

    `expected_services` is the fan-out list used when the endpoint is
    unreachable (defaults to PROXY_INTERNAL_SERVICES)."""
    started = time.perf_counter()
    expected = expected_services if expected_services is not None else PROXY_INTERNAL_SERVICES
    url = f"{base_url.rstrip('/')}/status/api"
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT})
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if r.status_code != 200:
            return _all_unknown(elapsed_ms, f"HTTP {r.status_code}", expected), None
        body = r.json()
    except httpx.TimeoutException:
        return _all_unknown(int((time.perf_counter() - started) * 1000), "timeout", expected), None
    except (httpx.HTTPError, ValueError) as exc:
        return _all_unknown(int((time.perf_counter() - started) * 1000), str(exc)[:200], expected), None

    out: list[ProbeResult] = []
    current = body.get("current") or {}

    # The platform returns a dict keyed by service name:
    #   {"current": {"Bot": {"status": "operational", "response_ms": 12}, ...}}
    # Tolerate the legacy list shape too in case the schema changes back.
    if isinstance(current, dict):
        items = current.items()
    else:
        items = (((e.get("name") if isinstance(e, dict) else "?"), e) for e in current)

    for name, entry in items:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status") or "unknown"
        response_ms = entry.get("response_ms")
        out.append(ProbeResult(
            service_name=name,
            status=status if status in ("operational", "degraded", "down", "unknown") else "unknown",
            response_ms=response_ms,
            source="proxy",
        ))
    return out, body


async def probe_status_shards(client: httpx.AsyncClient, base_url: str) -> dict[str, Any] | None:
    """Fetch the shard summary endpoint. Returns parsed JSON or None on
    failure. Caller stores the latest snapshot for the page to render."""
    url = f"{base_url.rstrip('/')}/status/api/shards"
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def _all_unknown(elapsed_ms: int, error: str, expected_services: list[str] | None = None) -> list[ProbeResult]:
    """Proxy unreachable — emit one ``unknown`` result per internal service so
    the page reflects "we don't know" immediately, instead of letting stale
    `operational` rows linger for the 5-minute aggregator stale-out window."""
    return [
        ProbeResult(
            service_name=name,
            status="unknown",
            response_ms=elapsed_ms,
            error=error,
            source="proxy",
        )
        for name in (expected_services if expected_services is not None else PROXY_INTERNAL_SERVICES)
    ]
