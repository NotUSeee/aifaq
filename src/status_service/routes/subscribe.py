"""Public webhook-subscription endpoints.

POST /subscribe/webhook       — register a Discord webhook (form field `url`)
GET  /subscribe/unsubscribe   — confirm page for an unsubscribe token
POST /subscribe/unsubscribe   — actually remove the subscription
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import subscribers
from ..ratelimit import limiter as _limiter

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


def _page(title: str, body_html: str) -> HTMLResponse:
    return HTMLResponse(
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'/>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
        f"<title>{title}</title>"
        "<link rel='stylesheet' href='/static/status.css?v=13'/></head>"
        "<body style='display:flex;align-items:center;justify-content:center;min-height:100vh;'>"
        f"<div style='max-width:420px;padding:2rem;text-align:center;'>{body_html}</div>"
        "</body></html>"
    )


@router.get("/unsubscribe", include_in_schema=False)
@_limiter.limit("30/minute")
async def unsubscribe_confirm(request: Request, token: str = ""):
    # GET renders a confirm form — never mutate on GET (link scanners
    # would silently unsubscribe people).
    if not token:
        return _page("Unsubscribe", "<h1>Missing token</h1>")
    return _page(
        "Unsubscribe — YourBot Status",
        "<h1>Unsubscribe?</h1>"
        "<p>This webhook will stop receiving YourBot status announcements.</p>"
        f"<form method='post' action='/subscribe/unsubscribe'>"
        f"<input type='hidden' name='token' value='{html.escape(token[:64], quote=True)}'/>"
        "<button type='submit'>Unsubscribe</button></form>",
    )


@router.post("/unsubscribe", include_in_schema=False)
@_limiter.limit("30/minute")
async def unsubscribe(request: Request, token: str = Form("")):
    removed = subscribers.remove_subscriber_by_token((token or "").strip())
    if removed:
        return _page("Unsubscribed — YourBot Status",
                     "<h1>Unsubscribed</h1><p>That webhook will no longer receive status updates.</p>")
    return _page("Unsubscribe — YourBot Status",
                 "<h1>Already gone</h1><p>That subscription no longer exists.</p>")
