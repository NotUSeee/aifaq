from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from status_service import db
from status_service.alerter import Alerter
from status_service.config import get_settings, reset_settings


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_alerter_disabled_when_no_webhook(monkeypatch):
    monkeypatch.setenv("ALERT_DISCORD_WEBHOOK_URL", "")
    reset_settings()
    async with httpx.AsyncClient() as client:
        a = Alerter(get_settings(), client)
        assert not a.enabled
        await a.evaluate([])  # must not raise


@pytest.mark.asyncio
async def test_alerter_fires_when_threshold_exceeded(monkeypatch):
    monkeypatch.setenv("ALERT_DISCORD_WEBHOOK_URL", "https://discord.test/webhook")
    monkeypatch.setenv("ALERT_THRESHOLD_MIN", "3")
    reset_settings()

    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO incidents(service_name, started_at, resolved) VALUES (?,?,0)",
            ("Public Site", _iso(started)),
        )

    with respx.mock(assert_all_called=False) as router:
        post = router.post("https://discord.test/webhook").mock(return_value=httpx.Response(204))
        async with httpx.AsyncClient() as client:
            a = Alerter(get_settings(), client)
            await a.evaluate([])
        assert post.called


@pytest.mark.asyncio
async def test_alerter_suppresses_within_cooldown(monkeypatch):
    monkeypatch.setenv("ALERT_DISCORD_WEBHOOK_URL", "https://discord.test/webhook")
    monkeypatch.setenv("ALERT_THRESHOLD_MIN", "1")
    monkeypatch.setenv("ALERT_COOLDOWN_MIN", "15")
    reset_settings()

    started = datetime.now(timezone.utc) - timedelta(minutes=10)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO incidents(service_name, started_at, resolved) VALUES (?,?,0)",
            ("Bot", _iso(started)),
        )
        conn.execute(
            "INSERT INTO alert_state(service_name, last_alert_at, last_status) VALUES (?,?,?)",
            ("Bot", _iso(datetime.now(timezone.utc) - timedelta(minutes=2)), "down"),
        )

    with respx.mock(assert_all_called=False) as router:
        post = router.post("https://discord.test/webhook").mock(return_value=httpx.Response(204))
        async with httpx.AsyncClient() as client:
            a = Alerter(get_settings(), client)
            await a.evaluate([])
        assert not post.called


@pytest.mark.asyncio
async def test_alerter_does_not_fire_under_threshold(monkeypatch):
    monkeypatch.setenv("ALERT_DISCORD_WEBHOOK_URL", "https://discord.test/webhook")
    monkeypatch.setenv("ALERT_THRESHOLD_MIN", "5")
    reset_settings()

    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO incidents(service_name, started_at, resolved) VALUES (?,?,0)",
            ("Bot", _iso(started)),
        )

    with respx.mock(assert_all_called=False) as router:
        post = router.post("https://discord.test/webhook").mock(return_value=httpx.Response(204))
        async with httpx.AsyncClient() as client:
            a = Alerter(get_settings(), client)
            await a.evaluate([])
        assert not post.called
