from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..ratelimit import limiter as _limiter
from .. import db
from ..aggregator import (
    SERVICE_ORDER,
    group_currents,
    incidents_recent,
    latest_per_service,
    overall_status,
    sla_summary,
)
from ..config import get_settings

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.globals["get_brand"] = get_settings


def _open_announcements() -> tuple[list[dict], list[dict]]:
    """Unresolved announcements split into (active, upcoming).

    An announcement with a future starts_at is scheduled maintenance that
    hasn't begun — shown in its own calmer "Scheduled" section instead of
    an alarming live banner. Everything else is active now.
    """
    now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, type, severity, title, body, created_at, starts_at, ends_at
            FROM announcements
            WHERE resolved_at IS NULL
            ORDER BY created_at DESC
            """
        ).fetchall()
        active: list[dict] = []
        upcoming: list[dict] = []
        for r in rows:
            updates = conn.execute(
                "SELECT status, body, created_at FROM announcement_updates "
                "WHERE announcement_id=? ORDER BY created_at ASC",
                (r["id"],),
            ).fetchall()
            item = {**dict(r), "updates": [dict(u) for u in updates]}
            if r["starts_at"] and r["starts_at"] > now_iso:
                upcoming.append(item)
            else:
                active.append(item)
        # Soonest-starting first for the schedule list.
        upcoming.sort(key=lambda a: a["starts_at"])
    return active, upcoming


@router.get("/", response_class=HTMLResponse)
@_limiter.limit("60/minute")
async def index(request: Request):
    settings = get_settings()
    currents = latest_per_service()
    sla = sla_summary(settings.sla_target_pct)
    active_announcements, upcoming_maintenance = _open_announcements()
    return templates.TemplateResponse(
        request,
        "status.html",
        context={
            "request": request,
            "now": datetime.now(timezone.utc),
            "service_order": SERVICE_ORDER,
            "currents": currents,
            "component_groups": group_currents(currents),
            "overall": overall_status(currents),
            "incidents": incidents_recent(days=7),
            "announcements": active_announcements,
            "upcoming_maintenance": upcoming_maintenance,
            "sla": sla,
            "uptime_90d": sla_summary(settings.sla_target_pct, days=90)["actual_pct"],
            "settings": settings,
        },
    )
