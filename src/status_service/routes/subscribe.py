"""Public webhook-subscription endpoints.

POST /subscribe/webhook       — register a Discord webhook (form field `url`)
GET  /subscribe/unsubscribe   — confirm page for an unsubscribe token
POST /subscribe/unsubscribe   — actually remove the subscription
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import subscribers
from ..config import get_settings
from ..ratelimit import limiter as _limiter
from .ui import templates

router = APIRouter(prefix="/subscribe")


@router.post("/webhook", include_in_schema=False)
@_limiter.limit("5/minute")
async def subscribe_webhook(request: Request, url: str = Form("")):
    url = (url or "").strip()
    if not subscribers.is_valid_webhook_url(url):
        return RedirectResponse("/?sub=invalid#subscribe", status_code=303)
    state, token = subscribers.add_subscriber(url)
    if state != "ok":
        return RedirectResponse(f"/?sub={state}#subscribe", status_code=303)
    # Prove deliverability immediately — a webhook Discord rejects is
    # useless, so drop it again rather than let it rot in the table.
    if not await subscribers.send_test_message(url, token):
        subscribers.remove_subscriber_by_token(token)
        return RedirectResponse("/?sub=unreachable#subscribe", status_code=303)
    return RedirectResponse("/?sub=ok#subscribe", status_code=303)


def _page(request: Request, title: str, heading: str, text: str = "", form_token: str = "") -> HTMLResponse:
    """A small message page in the same shell and theme as the status page."""
    return templates.TemplateResponse(request, "message.html", context={
        "request": request, "title": title, "heading": heading, "text": text,
        "form_token": form_token, "settings": get_settings(),
        "year": datetime.now(timezone.utc).year,
    })


@router.get("/unsubscribe", include_in_schema=False)
@_limiter.limit("30/minute")
async def unsubscribe_confirm(request: Request, token: str = ""):
    # GET renders a confirm form — never mutate on GET (link scanners
    # would silently unsubscribe people).
    if not token:
        return _page(request, "Unsubscribe · YourBot Status", "Missing token",
                     "This unsubscribe link is incomplete. Use the link from a status message.")
    return _page(
        request, "Unsubscribe · YourBot Status", "Unsubscribe?",
        "This webhook will stop receiving YourBot status announcements.",
        form_token=token[:64],
    )


@router.post("/unsubscribe", include_in_schema=False)
@_limiter.limit("30/minute")
async def unsubscribe(request: Request, token: str = Form("")):
    removed = subscribers.remove_subscriber_by_token((token or "").strip())
    if removed:
        return _page(request, "Unsubscribed · YourBot Status", "Unsubscribed",
                     "That webhook will no longer receive status updates.")
    return _page(request, "Unsubscribe · YourBot Status", "Already gone",
                 "That subscription no longer exists.")
