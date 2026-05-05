from __future__ import annotations

import time
from typing import Any

import httpx

from . import USER_AGENT, ProbeResult

DEGRADED_THRESHOLD_MS = 2000


def _classify(elapsed_ms: int, http_status: int) -> str:
    if 200 <= http_status < 300 and elapsed_ms < DEGRADED_THRESHOLD_MS:
        return "operational"
    if 200 <= http_status < 300:
        return "degraded"
    return "down"


async def probe_health(client: httpx.AsyncClient, base_url: str) -> ProbeResult:
    """Hit the public dashboard's /health endpoint. Maps to 'Public Site'."""
    return await _probe_one(client, base_url, "/health", "Public Site")


async def probe_readiness(client: httpx.AsyncClient, base_url: str) -> tuple[ProbeResult, dict[str, Any]]:
    """Hit /readiness which returns dependency status (db, redis).
    Returns the parent probe + the parsed body so the scheduler can
    derive db/redis ProbeResults from the same response."""
    started = time.perf_counter()
    url = f"{base_url.rstrip('/')}/readiness"
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT})
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body: dict[str, Any] = {}
        try:
            body = r.json()
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
                error=str(exc)[:200],
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
            error=str(exc)[:200],
            source="external",
        )


def derive_db_redis(parent: ProbeResult, body: dict) -> list[ProbeResult]:
    """The platform's /readiness response includes db/redis fields
    (`"ok"` | `"timeout"` | `"error"` | `"unavailable"`). Map them to
    individual ProbeResults so the page shows a row per dependency."""
    out: list[ProbeResult] = []
    for service_name, key in (("Database", "db"), ("Cache", "redis")):
        if parent.status == "down":
            # Match the visitor-facing reality: if the public site is down,
            # nobody can reach the database or cache through it. Mark `down`
            # rather than `unknown` so the page reads truthfully and uptime%
            # accumulates the outage. Same rationale as scheduler's per-
            # service down-write when readiness fails.
            out.append(ProbeResult(
                service_name=service_name,
                status="down",
                error="public site unreachable",
                source="external",
            ))
            continue
        raw = body.get(key) if isinstance(body, dict) else None
        if raw == "ok":
            status = "operational"
        elif raw in ("timeout", "error"):
            status = "down"
        elif raw == "unavailable" or raw is None:
            status = "unknown"
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
