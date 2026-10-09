from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..ratelimit import limiter as _limiter
from .. import db
from ..aggregator import (
    SERVICE_ORDER,
    _parse_iso,
    event_duration_text,
    format_duration,
    incident_events,
    newest_probe_at,
)
from ..chart import build_response_chart
from ..config import get_settings
from ..snapshot import get_snapshot

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.globals["get_brand"] = get_settings


def _utc_human(iso: str | None) -> str:
    """Readable UTC timestamp used as the no-script fallback inside <time>
    elements (the script rewrites them to the visitor's local time)."""
    if not iso:
        return ""
    try:
        dt = _parse_iso(iso).astimezone(timezone.utc)
    except Exception:
        return str(iso)
    return f"{dt.strftime('%b')} {dt.day}, {dt.year} {dt.strftime('%H:%M')} UTC"


templates.env.filters["utc_human"] = _utc_human
templates.env.filters["duration"] = format_duration
templates.env.filters["event_duration"] = event_duration_text


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


def _history_months(days: int = 90) -> list[dict]:
    """Announcements + incident events from the last N days, merged and
    grouped by calendar month (newest first) for /history."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    items: list[dict] = []

    for ev in incident_events(days=days, max_count=500):
        items.append({"kind": "incident", "date": ev["started_at"] or "", **ev})

    with db.connect() as conn:
        anns = conn.execute(
            "SELECT id, type, severity, title, body, created_at, resolved_at, starts_at, ends_at "
            "FROM announcements WHERE created_at >= ? ORDER BY created_at DESC LIMIT 200",
            (cutoff,),
        ).fetchall()
        for a in anns:
            updates = conn.execute(
                "SELECT status, body, created_at FROM announcement_updates "
                "WHERE announcement_id=? ORDER BY created_at ASC", (a["id"],),
            ).fetchall()
            items.append({
                "kind": "announcement",
                "date": a["created_at"] or "",
                **dict(a),
                "updates": [dict(u) for u in updates],
            })

    items.sort(key=lambda i: i["date"], reverse=True)
    months: list[dict] = []
    for item in items:
        ym = item["date"][:7] if len(item["date"]) >= 7 else "unknown"
        if not months or months[-1]["ym"] != ym:
            try:
                label = datetime.strptime(ym, "%Y-%m").strftime("%B %Y")
            except ValueError:
                label = "Earlier"
            months.append({"ym": ym, "label": label, "items": []})
        months[-1]["items"].append(item)
    return months


_AGE_PLACEHOLDER = "__AGE_SECONDS__"


def _age_seconds() -> str:
    """Seconds since the newest check, as text for the page script. Read
    fresh on every response (it is one indexed lookup) so the "last checked"
    counter is right even when the rest of the page comes from cache."""
    newest = newest_probe_at()
    if newest is None:
        return ""
    return f"{max(0.0, (datetime.now(timezone.utc) - newest).total_seconds()):.1f}"


def _page_context(request: Request, age_attr: str | None = None) -> dict:
    settings = get_settings()
    active_announcements, upcoming_maintenance = _open_announcements()
    return {
        "request": request,
        "now": datetime.now(timezone.utc),
        "snap": get_snapshot(),
        "age_attr": _age_seconds() if age_attr is None else age_attr,
        "events": incident_events(days=7),
        "chart": build_response_chart(hours=24),
        "announcements": active_announcements,
        "upcoming_maintenance": upcoming_maintenance,
        "service_order": SERVICE_ORDER,
        "settings": settings,
        "sub": request.query_params.get("sub"),
    }


@router.get("/history", response_class=HTMLResponse)
@_limiter.limit("60/minute")
async def history(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "history.html",
        context={
            "request": request,
            "now": datetime.now(timezone.utc),
            "months": _history_months(days=90),
            "settings": settings,
        },
    )


@router.get("/", response_class=HTMLResponse)
@_limiter.limit("60/minute")
async def index(request: Request):
    return templates.TemplateResponse(request, "status.html", context=_page_context(request))


# The live regions of the page as one HTML fragment. The page script swaps
# them in every 15 seconds, so there is exactly one place (the templates)
# that decides how a status, a number or a day cell is drawn.
_live_cache: dict[str, tuple[float, str]] = {}


def clear_live_cache() -> None:
    _live_cache.clear()


@router.get("/live", response_class=HTMLResponse, include_in_schema=False)
@_limiter.limit("120/minute")
async def live(request: Request):
    settings = get_settings()
    ttl = max(0.0, float(settings.api_cache_seconds))
    hit = _live_cache.get(settings.db_path)
    now = time.monotonic()
    if hit and ttl > 0 and now - hit[0] < ttl:
        html = hit[1]
    else:
        html = templates.get_template("_live.html").render(**_page_context(request, age_attr=_AGE_PLACEHOLDER))
        if ttl > 0:
            _live_cache[settings.db_path] = (now, html)
    return HTMLResponse(html.replace(_AGE_PLACEHOLDER, _age_seconds()))
