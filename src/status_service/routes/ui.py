from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address

from .. import db
from ..aggregator import (
    SERVICE_ORDER,
    incidents_recent,
    latest_per_service,
    overall_status,
    sla_summary,
)
from ..config import get_settings

router = APIRouter()
_limiter = Limiter(key_func=get_remote_address)

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _active_announcements() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, type, severity, title, body, created_at
            FROM announcements
            WHERE resolved_at IS NULL
            ORDER BY created_at DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            updates = conn.execute(
                "SELECT status, body, created_at FROM announcement_updates "
                "WHERE announcement_id=? ORDER BY created_at ASC",
                (r["id"],),
            ).fetchall()
            out.append({
                **dict(r),
                "updates": [dict(u) for u in updates],
            })
    return out


@router.get("/", response_class=HTMLResponse)
@_limiter.limit("60/minute")
async def index(request: Request):
    settings = get_settings()
    currents = latest_per_service()
    return templates.TemplateResponse(
        request,
        "status.html",
        context={
            "request": request,
            "now": datetime.now(timezone.utc),
            "service_order": SERVICE_ORDER,
            "currents": currents,
            "overall": overall_status(currents),
            "incidents": incidents_recent(days=7),
            "announcements": _active_announcements(),
            "sla": sla_summary(settings.sla_target_pct),
            "settings": settings,
        },
    )
