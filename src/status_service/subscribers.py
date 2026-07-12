"""Discord-webhook subscriptions: visitors register their own server's
webhook and receive announcement broadcasts (created / updated / resolved).

Only Discord webhook URLs are accepted — this service must never become
an open POST-anywhere relay. Dead webhooks self-prune: a 401/403/404/410
removes the subscriber immediately, and 10 consecutive failures of any
kind removes it too.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets

import httpx

from . import db
from .config import get_settings

logger = logging.getLogger("status_service.subscribers")

DISCORD_WEBHOOK_RE = re.compile(
    r"^https://(?:ptb\.|canary\.)?(?:discord\.com|discordapp\.com)/api/webhooks/\d+/[A-Za-z0-9_.-]+$"
)

SEVERITY_COLOR = {"info": 0x3B82F6, "warning": 0xE0A33E, "critical": 0xE05A5A}
_MAX_FAILURES = 10
_DEAD_STATUS = {401, 403, 404, 410}
_CONCURRENCY = 10


def is_valid_webhook_url(url: str) -> bool:
    return bool(DISCORD_WEBHOOK_RE.match((url or "").strip()))


def add_subscriber(url: str) -> tuple[str, str | None]:
    """Register a webhook. Returns (state, token) where state is one of
    "ok", "dup", "full"."""
    url = url.strip()
    settings = get_settings()
    token = secrets.token_urlsafe(24)
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM webhook_subscribers").fetchone()["n"]
        if int(count) >= settings.max_webhook_subscribers:
            return "full", None
        dup = conn.execute("SELECT 1 AS x FROM webhook_subscribers WHERE url=?", (url,)).fetchone()
        if dup:
            return "dup", None
        conn.execute(
            "INSERT INTO webhook_subscribers(url, token) VALUES (?,?)", (url, token))
    return "ok", token


def remove_subscriber_by_token(token: str) -> bool:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM webhook_subscribers WHERE token=?", (token,))
        return (cur.rowcount or 0) > 0


def _remove_by_url(url: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM webhook_subscribers WHERE url=?", (url,))


def _bump_failures(url: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE webhook_subscribers SET failures = failures + 1 WHERE url=?", (url,))
        conn.execute(
            "DELETE FROM webhook_subscribers WHERE url=? AND failures >= ?", (url, _MAX_FAILURES))


def _reset_failures(url: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE webhook_subscribers SET failures = 0 WHERE url=?", (url,))


def _unsub_url(token: str) -> str:
    base = get_settings().status_public_url.rstrip("/")
    return f"{base}/subscribe/unsubscribe?token={token}"


def build_announcement_embed(kind: str, severity: str, title: str, body: str,
                             unsub_token: str | None = None) -> dict:
    settings = get_settings()
    embed = {
        "title": f"{kind}: {title}",
        "description": body[:3500],
        "color": SEVERITY_COLOR.get(severity, SEVERITY_COLOR["info"]),
        "url": settings.status_public_url,
        "footer": {"text": "YourBot status"},
    }
    if unsub_token:
        embed["description"] += f"\n\n-# [Unsubscribe]({_unsub_url(unsub_token)})"
    return embed


async def send_test_message(url: str, token: str) -> bool:
    """Deliverability check right after subscribing. Returns False (and the
    caller should drop the row) when Discord rejects the webhook."""
    embed = {
        "title": "Subscribed to YourBot status updates",
        "description": (
            "This webhook will receive incident and maintenance announcements.\n\n"
            f"-# [Unsubscribe]({_unsub_url(token)})"
        ),
        "color": SEVERITY_COLOR["info"],
        "url": get_settings().status_public_url,
        "footer": {"text": "YourBot status"},
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json={"embeds": [embed]}, timeout=8.0)
        return r.status_code < 400
    except httpx.HTTPError:
        return False


async def broadcast_announcement(kind: str, severity: str, title: str, body: str) -> int:
    """Deliver an announcement embed to every subscriber. Returns the
    number of successful deliveries."""
    with db.connect() as conn:
        subs = [dict(r) for r in conn.execute(
            "SELECT url, token FROM webhook_subscribers").fetchall()]
    if not subs:
        return 0

    sem = asyncio.Semaphore(_CONCURRENCY)
    delivered = 0

    async with httpx.AsyncClient() as client:
        async def _send(sub: dict) -> bool:
            embed = build_announcement_embed(kind, severity, title, body, sub["token"])
            async with sem:
                try:
                    r = await client.post(sub["url"], json={"embeds": [embed]}, timeout=8.0)
                except httpx.HTTPError:
                    _bump_failures(sub["url"])
                    return False
            if r.status_code in _DEAD_STATUS:
                _remove_by_url(sub["url"])
                return False
            if r.status_code >= 400:
                _bump_failures(sub["url"])
                return False
            _reset_failures(sub["url"])
            return True

        results = await asyncio.gather(*(_send(s) for s in subs), return_exceptions=True)
    delivered = sum(1 for ok in results if ok is True)
    logger.info("broadcast '%s' delivered to %d/%d subscribers", title, delivered, len(subs))
    return delivered
