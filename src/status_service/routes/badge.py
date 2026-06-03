from __future__ import annotations

from fastapi import APIRouter, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address

from ..aggregator import latest_per_service, overall_status

router = APIRouter()
_limiter = Limiter(key_func=get_remote_address)

LABEL_FOR = {
    "operational": ("operational", "#6bcb8b"),
    "degraded":    ("degraded",    "#e0a33e"),
    "partial_outage": ("partial outage", "#e0a33e"),
    "outage":      ("outage",      "#e05a5a"),
    "unknown":     ("unknown",     "#888888"),
}


def _svg(left: str, right: str, color: str) -> str:
    """Compact Shields.io-style flat badge. Pixel widths are approximate
    but cover the longest label ('partial outage'). Renders crisply at
    any DPI thanks to text via system-font fallback."""
    left_w = 6 * len(left) + 16
    right_w = 6 * len(right) + 16
    total = left_w + right_w
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{left}: {right}">
  <linearGradient id="b" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <mask id="a"><rect width="{total}" height="20" rx="3" fill="#fff"/></mask>
  <g mask="url(#a)">
    <rect width="{left_w}" height="20" fill="#555"/>
    <rect x="{left_w}" width="{right_w}" height="20" fill="{color}"/>
    <rect width="{total}" height="20" fill="url(#b)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="DejaVu Sans,Verdana,Geneva,sans-serif" font-size="11">
    <text x="{left_w / 2}" y="14">{left}</text>
    <text x="{left_w + right_w / 2}" y="14">{right}</text>
  </g>
</svg>"""


@router.get("/badge.svg")
@_limiter.limit("60/minute")
async def badge(request: Request) -> Response:
    overall = overall_status(latest_per_service())
    label, color = LABEL_FOR.get(overall, LABEL_FOR["unknown"])
    body = _svg("yourbot", label, color)
    return Response(
        content=body,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "public, max-age=30",
        },
    )
