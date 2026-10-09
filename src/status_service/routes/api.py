from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..ratelimit import limiter as _limiter
from ..aggregator import (
    SERVICE_ORDER,
    daily_uptime_series,
    incident_events,
    incidents_recent,
    response_time_series,
    shard_summary,
    sla_summary,
)
from ..config import get_settings
from ..snapshot import get_snapshot

router = APIRouter()


def _public_component(c: dict) -> dict:
    """A component as the JSON API exposes it (no rendering extras)."""
    return {
        "key": c["key"],
        "name": c["name"],
        "description": c["blurb"],
        "status": c["status"],
        "third_party": c["third_party"],
        "counts_toward_overall_uptime": c["core"],
        # Someone else's service: we relay its status, we do not publish an
        # availability figure for it.
        "uptime": None if c["third_party"] else c["uptime"],
        "checks": [
            {
                "name": k["name"],
                "label": k["label"],
                "description": k["what"],
                "measured": k["how"],
                "counts_toward_uptime": not k["advisory"],
                "status": k["status"],
                "note": k["note"],
                "checked_at": k["checked_at"],
                "uptime": None if c["third_party"] else k["uptime"],
            }
            for k in c["checks"]
        ],
    }


@router.get("/api")
@_limiter.limit("120/minute")
async def api_current(request: Request) -> JSONResponse:
    """Current status. `current`, `overall` and `service_order` mirror
    YourBot's own /status/api; `components` is the customer-facing view
    with uptime per period; `meta` carries freshness."""
    settings = get_settings()
    snap = get_snapshot()
    payload = {
        "overall": snap["overall"],
        "headline": snap["headline"],
        "detail": snap["detail"],
        "components": [_public_component(c) for c in snap["components"] + snap["third_party"]],
        "uptime": snap["uptime"],
        "current": [
            {
                "name": c.name,
                "status": c.status,
                "response_ms": c.response_ms,
                "checked_at": c.checked_at,
                "error": c.error,
            }
            for c in snap["currents"]
        ],
        "service_order": SERVICE_ORDER,
        "meta": {
            "staleness_seconds": snap["staleness_seconds"],
            "probe_interval_seconds": settings.probe_interval_seconds,
            "monitor_online": snap["monitor"]["online"],
            "monitor_offline_since": snap["monitor"]["offline_since"],
            "data_since": snap["data_since"],
            "sla": sla_summary(settings.sla_target_pct),
            "now": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        },
    }
    return JSONResponse(payload)


@router.get("/api/graph")
@_limiter.limit("30/minute")
async def api_graph(request: Request, hours: int = 24) -> JSONResponse:
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
    """`events` groups what happened into one entry per event; `incidents`
    keeps the raw per-check rows."""
    days = max(1, min(90, int(days)))
    return JSONResponse({
        "days": days,
        "events": incident_events(days=days, max_count=100),
        "incidents": incidents_recent(days=days),
    })
