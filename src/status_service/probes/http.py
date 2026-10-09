from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from . import USER_AGENT, ProbeResult

DEGRADED_THRESHOLD_MS = 2000

# Written on rows for services we could not look at this cycle because the
# website (our only window into the platform) was unreachable. remeasure.py
# matches the pre-rework spelling of this to find old guessed rows.
NOT_MEASURED_ERROR = "not measured: website unreachable"


def _classify(elapsed_ms: int, http_status: int) -> str:
    if 200 <= http_status < 300 and elapsed_ms < DEGRADED_THRESHOLD_MS:
        return "operational"
    if 200 <= http_status < 300:
        return "degraded"
    return "down"


async def probe_health(client: httpx.AsyncClient, base_url: str) -> ProbeResult:
    """Hit the public dashboard's /health endpoint. Maps to 'Public Site'."""
    return await _probe_one(client, base_url, "/health", "Public Site")


async def probe_readiness(
    client: httpx.AsyncClient,
    base_url: str,
    attempts: int = 1,
    retry_delay: float = 2.0,
) -> tuple[ProbeResult, dict[str, Any]]:
    """Hit /readiness, which returns dependency status (db, redis).

    Returns the parent probe plus the parsed body so the scheduler can
    derive db/redis ProbeResults from the same response.

    One failed request is not evidence of an outage. Over 2026-10-04..06
    this probe "failed" 11 times, each a single cycle, and the load
    balancer's own log shows none of those requests ever arrived: they
    died on the monitor's side of the internet while the site answered
    every request that reached it. So a failure is retried up to
    ``attempts`` times, ``retry_delay`` seconds apart (growing a little
    each time), and only reported when every attempt failed. Anything
    genuinely down fails the retries too.
    """
    attempts = max(1, int(attempts))
    result, body = await _readiness_once(client, base_url)
    tries = 1
    while result.status == "down" and tries < attempts:
        await asyncio.sleep(max(0.0, retry_delay) * tries)
        result, body = await _readiness_once(client, base_url)
        tries += 1
    if result.status == "down":
        result.extra = {**(result.extra or {}), "attempts": tries}
    return result, body


async def _readiness_once(client: httpx.AsyncClient, base_url: str) -> tuple[ProbeResult, dict[str, Any]]:
    started = time.perf_counter()
    url = f"{base_url.rstrip('/')}/readiness"
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT})
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body: dict[str, Any] = {}
        try:
            parsed = r.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            pass
        status = _classify(elapsed_ms, r.status_code)
        result = ProbeResult(
            service_name="Public Site",
            status=status,
            response_ms=elapsed_ms,
            http_status=r.status_code,
            source="external",
            extra=body,
            error=None if status != "down" else f"HTTP {r.status_code}",
        )
        return result, body
    except httpx.TimeoutException:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return (
            ProbeResult(
                service_name="Public Site",
                status="down",
                response_ms=elapsed_ms,
                error="timeout",
                source="external",
            ),
            {},
        )
    except httpx.HTTPError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return (
            ProbeResult(
                service_name="Public Site",
                status="down",
                response_ms=elapsed_ms,
                error=(str(exc) or type(exc).__name__)[:200],
                source="external",
            ),
            {},
        )


async def _probe_one(
    client: httpx.AsyncClient,
    base_url: str,
    path: str,
    service_name: str,
) -> ProbeResult:
    started = time.perf_counter()
    url = f"{base_url.rstrip('/')}{path}"
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT})
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(
            service_name=service_name,
            status=_classify(elapsed_ms, r.status_code),
            response_ms=elapsed_ms,
            http_status=r.status_code,
            source="external",
        )
    except httpx.TimeoutException:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(
            service_name=service_name,
            status="down",
            response_ms=elapsed_ms,
            error="timeout",
            source="external",
        )
    except httpx.HTTPError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(
            service_name=service_name,
            status="down",
            response_ms=elapsed_ms,
            error=(str(exc) or type(exc).__name__)[:200],
            source="external",
        )


def derive_db_redis(parent: ProbeResult, body: dict) -> list[ProbeResult]:
    """The platform's /readiness response includes db/redis fields
    (`"ok"` | `"timeout"` | `"error"` | `"unavailable"`). Map them to
    individual ProbeResults so the page shows a row per dependency.

    When the parent probe failed we learned NOTHING about the database or
    the cache, so they are reported `unknown`, not `down`. They used to be
    written `down` on the theory that a visitor could not reach them
    either, which published two outages per website blip for services
    whose own records show them up the whole time.
    """
    out: list[ProbeResult] = []
    for service_name, key in (("Database", "db"), ("Cache", "redis")):
        if parent.status in ("down", "unknown"):
            out.append(ProbeResult(
                service_name=service_name,
                status="unknown",
                error=NOT_MEASURED_ERROR,
                source="external",
            ))
            continue
        raw = body.get(key) if isinstance(body, dict) else None
        if raw == "ok":
            status = "operational"
        elif raw in ("timeout", "error"):
            status = "down"
        else:
            status = "unknown"
        out.append(ProbeResult(
            service_name=service_name,
            status=status,
            response_ms=parent.response_ms,
            source="external",
            error=None if status == "operational" else (raw or "no data"),
        ))
    return out
