"""Public RSS feed of status updates — announcements (incidents/maintenance,
with their update threads) and explained/resolved auto-detected incidents.
Lets people subscribe in any RSS reader to follow the platform's status."""

from __future__ import annotations

from xml.sax.saxutils import escape

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import db
from ..aggregator import _parse_iso, incidents_recent
from ..ratelimit import limiter as _limiter

router = APIRouter()


def _esc(s) -> str:
    return escape(str(s if s is not None else ""))


def _rfc822(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return _parse_iso(iso).strftime("%a, %d %b %Y %H:%M:%S +0000")
    except Exception:
        return ""


def _base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _item(title: str, desc: str, base: str, guid: str, date_iso: str | None) -> str:
    # guid doubles as the on-page anchor (e.g. /#announcement-3) so readers
    # land on the exact entry instead of the top of the page.
    return (
        "<item>"
        f"<title>{_esc(title)}</title>"
        f"<link>{_esc(base)}/#{_esc(guid)}</link>"
        f'<guid isPermaLink="false">{_esc(guid)}</guid>'
        f"<pubDate>{_rfc822(date_iso)}</pubDate>"
        f"<description>{_esc(desc)}</description>"
        "</item>"
    )


@router.get("/feed.xml", include_in_schema=False)
@router.get("/rss", include_in_schema=False)
@_limiter.limit("60/minute")
def feed(request: Request) -> Response:
    base = _base(request)
    entries: list[tuple[str, str]] = []  # (sort_iso, item_xml)

    with db.connect() as conn:
        anns = conn.execute(
            "SELECT id, type, severity, title, body, created_at, resolved_at, starts_at, ends_at "
            "FROM announcements ORDER BY created_at DESC LIMIT 40"
        ).fetchall()
        for a in anns:
            updates = conn.execute(
                "SELECT status, body, created_at FROM announcement_updates "
                "WHERE announcement_id=? ORDER BY created_at ASC", (a["id"],)
            ).fetchall()
            latest = a["created_at"]
            parts = [a["body"]]
            if a["type"] == "maintenance" and a["starts_at"]:
                window = f"Window: {a['starts_at']}"
                if a["ends_at"]:
                    window += f" to {a['ends_at']}"
                parts.insert(0, window + " (UTC)")
            for u in updates:
                parts.append(f"[{u['status']}] {u['body']}")
                if u["created_at"] and u["created_at"] > latest:
                    latest = u["created_at"]
            if a["resolved_at"] and a["resolved_at"] > latest:
                latest = a["resolved_at"]
            scheduled = bool(a["type"] == "maintenance" and a["starts_at"] and not a["resolved_at"])
            kind = "Scheduled maintenance" if scheduled else (
                "Maintenance" if a["type"] == "maintenance" else "Incident")
            suffix = " — Resolved" if a["resolved_at"] else ""
            entries.append((latest or "", _item(
                f"{kind}: {a['title']}{suffix}",
                "\n\n".join(p for p in parts if p),
                base, f"announcement-{a['id']}", latest or a["created_at"])))

    # Auto-detected incidents: include resolved outages and any with an
    # admin-written cause (skip unexplained ongoing ones — that's noise).
    for inc in incidents_recent(days=90, max_count=40):
        if not inc.get("resolved") and not inc.get("cause"):
            continue
        dur = inc.get("duration_min")
        if inc.get("resolved"):
            title = f"{inc['service_name']}: outage resolved" + (f" ({dur} min)" if dur else "")
        else:
            title = f"{inc['service_name']}: outage update"
        desc = inc.get("cause") or f"{inc['service_name']} experienced a service disruption."
        date_iso = inc.get("ended_at") or inc.get("started_at")
        entries.append((date_iso or "", _item(
            title, desc, base, f"incident-{inc['id']}", date_iso or inc.get("started_at"))))

    entries.sort(key=lambda e: e[0], reverse=True)
    items = "".join(x for _, x in entries[:40])

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
        "<title>YourBot Status</title>"
        f"<link>{_esc(base)}/</link>"
        f'<atom:link href="{_esc(base)}/feed.xml" rel="self" type="application/rss+xml"/>'
        "<description>Incidents and maintenance for the YourBot platform.</description>"
        "<language>en</language>"
        f"{items}"
        "</channel></rss>"
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")
