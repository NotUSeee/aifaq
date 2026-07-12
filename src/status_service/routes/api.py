from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..ratelimit import limiter as _limiter
from ..aggregator import (
    SERVICE_ORDER,
    daily_uptime_series,
    incidents_recent,
    latest_per_service,
    newest_probe_at,
    overall_status,
    response_time_series,
    shard_summary,
    sla_summary,
)
from ..config import get_settings

router = APIRouter()


def _staleness_seconds() -> float | None:
    newest = newest_probe_at()
    if newest is None:
        return None
    return (datetime.now(timezone.utc) - newest).total_seconds()


@router.get("/api")
@_limiter.limit("120/minute")
async def api_current(request: Request) -> JSONResponse:
    """Schema mirrors YourBot's /status/api so existing JS works:
    `{current: [...], overall: str, service_order: [...]}` plus a
    `meta` block with staleness and SLA info."""
    settings = get_settings()
    currents = latest_per_service()
    payload = {
        "current": [
            {
                "name": c.name,
                "status": c.status,
                "response_ms": c.response_ms,
                "checked_at": c.checked_at,
                "error": c.error,
            }
            for c in currents
        ],
        "overall": overall_status(currents),
        "service_order": SERVICE_ORDER,
        "meta": {
            "staleness_seconds": _staleness_seconds(),
            "probe_interval_seconds": settings.probe_interval_seconds,
            "sla": sla_summary(settings.sla_target_pct),
            "now": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        },
    }
    return JSONResponse(payload)


@router.get("/api/graph")
@_limiter.limit("30/minute")
async def api_graph(request: Request, hours: int = 6) -> JSONResponse:
    hours = max(1, min(24, int(hours)))
    return JSONResponse(response_time_series(hours=hours))


@router.get("/api/timeline")
@_limiter.limit("30/minute")
async def api_timeline(request: Request, days: int = 90) -> JSONResponse:
    days = max(1, min(180, int(days)))
    return JSONResponse(daily_uptime_series(days=days))


@router.get("/api/shards")
@_limiter.limit("120/minute")
async def api_shards(request: Request) -> JSONResponse:
    return JSONResponse(shard_summary())


@router.get("/api/incidents")
@_limiter.limit("30/minute")
async def api_incidents(request: Request, days: int = 7) -> JSONResponse:
    days = max(1, min(90, int(days)))
    return JSONResponse({"days": days, "incidents": incidents_recent(days=days)})
