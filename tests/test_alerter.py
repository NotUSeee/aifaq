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
    monkeypatch.setenv("ALERT_STYLE", "stream")
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
    monkeypatch.setenv("ALERT_STYLE", "stream")
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


# ── Status board (single edited message) ───────────────────────────────
def _insert_probe(service_name: str, status: str, source: str = "external"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO probe_results(service_name,status,response_ms,http_status,error,source) "
            "VALUES (?,?,?,?,?,?)",
            (service_name, status, 50, 200, None, source),
        )


def _board_env(monkeypatch):
    monkeypatch.setenv("ALERT_DISCORD_WEBHOOK_URL", "https://discord.test/webhook")
    reset_settings()  # ALERT_STYLE defaults to "board"


@pytest.mark.asyncio
async def test_board_creates_once_then_edits_on_change(monkeypatch):
    _board_env(monkeypatch)
    _insert_probe("Public Site", "operational")

    with respx.mock(assert_all_called=False) as router:
        post = router.post("https://discord.test/webhook").mock(
            return_value=httpx.Response(200, json={"id": "555"}))
        patch = router.patch("https://discord.test/webhook/messages/555").mock(
            return_value=httpx.Response(200, json={"id": "555"}))
        async with httpx.AsyncClient() as client:
            a = Alerter(get_settings(), client)
            await a.evaluate([])          # first cycle → creates the board
            assert post.call_count == 1
            assert db.kv_get("alert_board_message_id") == "555"

            await a.evaluate([])          # unchanged state → no calls at all
            assert post.call_count == 1
            assert not patch.called

            _insert_probe("Public Site", "down")   # state change → edit, not repost
            await a.evaluate([])
            assert patch.call_count == 1
            assert post.call_count == 1


@pytest.mark.asyncio
async def test_board_full_outage_is_one_message_not_a_flood(monkeypatch):
    """The exact spam scenario: every service down at once must produce a
    single webhook create, never one message per service."""
    _board_env(monkeypatch)
    for name in ("Public Site", "Dashboard", "Gateway", "Bot", "Bot Worker",
                 "Analytics", "Database", "Cache", "DNS"):
        _insert_probe(name, "down", source="proxy" if name not in ("Public Site", "DNS") else "external")

    with respx.mock(assert_all_called=False) as router:
        post = router.post("https://discord.test/webhook").mock(
            return_value=httpx.Response(200, json={"id": "777"}))
        async with httpx.AsyncClient() as client:
            a = Alerter(get_settings(), client)
            await a.evaluate([])
        assert post.call_count == 1


@pytest.mark.asyncio
async def test_board_reposts_when_message_deleted(monkeypatch):
    _board_env(monkeypatch)
    _insert_probe("Public Site", "operational")
    db.kv_set("alert_board_message_id", "999")

    with respx.mock(assert_all_called=False) as router:
        router.patch("https://discord.test/webhook/messages/999").mock(
            return_value=httpx.Response(404))
        post = router.post("https://discord.test/webhook").mock(
            return_value=httpx.Response(200, json={"id": "1000"}))
        async with httpx.AsyncClient() as client:
            a = Alerter(get_settings(), client)
            await a.evaluate([])
        assert post.called
    assert db.kv_get("alert_board_message_id") == "1000"


@pytest.mark.asyncio
async def test_alerter_does_not_fire_under_threshold(monkeypatch):
    monkeypatch.setenv("ALERT_DISCORD_WEBHOOK_URL", "https://discord.test/webhook")
    monkeypatch.setenv("ALERT_THRESHOLD_MIN", "5")
    monkeypatch.setenv("ALERT_STYLE", "stream")
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
