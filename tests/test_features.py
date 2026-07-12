"""History page, per-component badges, response-time percentiles,
webhook subscriptions, and the outage ping."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from status_service import db, subscribers
from status_service.aggregator import response_time_series
from status_service.alerter import Alerter
from status_service.config import get_settings, reset_settings
from status_service.main import app

SECRET = "test-secret-deadbeef"
GOOD_HOOK = "https://discord.com/api/webhooks/123456/abcDEF-token_x"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sign(body: bytes) -> dict[str, str]:
    ts = int(time.time())
    msg = f"{ts}.".encode("utf-8") + body
    sig = hmac.new(SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {"X-Status-Timestamp": str(ts), "X-Status-Signature": sig}


def _insert_probe(name: str, status: str, ms: int = 50, source: str = "external",
                  at: datetime | None = None):
    when = at or datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO probe_results(service_name,status,response_ms,http_status,error,source,checked_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, status, ms, 200, None, source, _iso(when)),
        )


# ── /history ─────────────────────────────────────────────────────────────
def test_history_page_shows_incidents_and_announcements():
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO incidents(service_name, started_at, ended_at, duration_min, resolved, cause) "
            "VALUES ('Gateway', ?, ?, 12, 1, 'Router firmware update went sideways.')",
            (_iso(now - timedelta(days=2)), _iso(now - timedelta(days=2) + timedelta(minutes=12))))
        conn.execute(
            "INSERT INTO announcements(type,severity,title,body,resolved_at) "
            "VALUES ('incident','warning','API slowdowns','Elevated latency.', ?)",
            (_iso(now - timedelta(days=1)),))
    with TestClient(app) as client:
        r = client.get("/history")
    assert r.status_code == 200
    html = r.text
    assert now.strftime("%B %Y") in html                      # month header
    assert "Gateway" in html
    assert "Router firmware update went sideways." in html    # cause surfaced
    assert "API slowdowns" in html
    assert 'id="incident-' in html and 'id="announcement-' in html


def test_main_page_links_to_history():
    with TestClient(app) as client:
        html = client.get("/").text
    assert 'href="/history"' in html


# ── per-component badge ──────────────────────────────────────────────────
def test_component_badge_reflects_service_status():
    _insert_probe("Bot", "down", source="proxy")
    with TestClient(app) as client:
        r = client.get("/badge/bot.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers["content-type"]
    assert "#e05a5a" in r.text and "down" in r.text


def test_component_badge_unknown_service_404():
    with TestClient(app) as client:
        assert client.get("/badge/not-a-service.svg").status_code == 404


# ── response-time percentiles ────────────────────────────────────────────
def test_response_time_series_reports_percentiles():
    now = datetime.now(timezone.utc)
    for i, ms in enumerate(range(100, 2100, 100)):  # 20 samples, same bucket-ish
        _insert_probe("Public Site", "operational", ms=ms, at=now - timedelta(seconds=i))
    data = response_time_series(hours=6)
    points = data["series"]["Public Site"]
    assert points, "expected at least one bucket"
    total_n = sum(p["n"] for p in points)
    assert total_n == 20
    for p in points:
        assert p["p95"] >= p["p50"] > 0
    # p95 must sit near the top of the distribution somewhere
    assert max(p["p95"] for p in points) >= 1900


def test_graph_endpoint_new_shape():
    _insert_probe("Public Site", "operational", ms=120)
    with TestClient(app) as client:
        body = client.get("/api/graph?hours=6").json()
    assert "bucket_seconds" in body
    pt = body["series"]["Public Site"][0]
    assert set(("t", "p50", "p95", "n")) <= set(pt)


# ── webhook subscriptions ────────────────────────────────────────────────
def test_subscribe_rejects_non_discord_url():
    with TestClient(app) as client:
        r = client.post("/subscribe/webhook", data={"url": "https://evil.example/collect"},
                        follow_redirects=False)
    assert r.status_code == 303 and "sub=invalid" in r.headers["location"]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM webhook_subscribers").fetchone()["n"] == 0


def test_subscribe_stores_and_sends_test_message():
    with respx.mock(assert_all_called=False) as router:
        hook = router.post(GOOD_HOOK).mock(return_value=httpx.Response(204))
        with TestClient(app) as client:
            r = client.post("/subscribe/webhook", data={"url": GOOD_HOOK}, follow_redirects=False)
            assert "sub=ok" in r.headers["location"]
            r2 = client.post("/subscribe/webhook", data={"url": GOOD_HOOK}, follow_redirects=False)
            assert "sub=dup" in r2.headers["location"]
    assert hook.call_count == 1  # one test message; dup didn't send again
    body = json.loads(hook.calls[0].request.content)
    assert "/subscribe/unsubscribe?token=" in body["embeds"][0]["description"]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM webhook_subscribers").fetchone()["n"] == 1


def test_subscribe_drops_webhook_discord_rejects():
    with respx.mock(assert_all_called=False) as router:
        router.post(GOOD_HOOK).mock(return_value=httpx.Response(404))
        with TestClient(app) as client:
            r = client.post("/subscribe/webhook", data={"url": GOOD_HOOK}, follow_redirects=False)
    assert "sub=unreachable" in r.headers["location"]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM webhook_subscribers").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_broadcast_delivers_and_prunes_dead_webhooks():
    ok_url = GOOD_HOOK
    dead_url = "https://discord.com/api/webhooks/999/dead-token"
    assert subscribers.add_subscriber(ok_url)[0] == "ok"
    assert subscribers.add_subscriber(dead_url)[0] == "ok"

    with respx.mock(assert_all_called=False) as router:
        ok_route = router.post(ok_url).mock(return_value=httpx.Response(204))
        router.post(dead_url).mock(return_value=httpx.Response(404))
        delivered = await subscribers.broadcast_announcement(
            "Incident", "critical", "DB down", "Investigating.")
    assert delivered == 1
    assert ok_route.called
    with db.connect() as conn:
        rows = [r["url"] for r in conn.execute("SELECT url FROM webhook_subscribers").fetchall()]
    assert rows == [ok_url]  # dead one pruned immediately on 404


def test_hmac_announce_broadcasts_to_subscribers():
    assert subscribers.add_subscriber(GOOD_HOOK)[0] == "ok"
    payload = json.dumps({"type": "incident", "severity": "critical",
                          "title": "Redis down", "body": "On it."}).encode()
    with respx.mock(assert_all_called=False) as router:
        hook = router.post(GOOD_HOOK).mock(return_value=httpx.Response(204))
        with TestClient(app) as client:
            r = client.post("/admin/announce", content=payload,
                            headers={**_sign(payload), "Content-Type": "application/json"})
    assert r.status_code == 200
    assert hook.called
    body = json.loads(hook.calls[0].request.content)
    assert "Incident: Redis down" in body["embeds"][0]["title"]


def test_unsubscribe_flow():
    state, token = subscribers.add_subscriber(GOOD_HOOK)
    assert state == "ok"
    with TestClient(app) as client:
        page = client.get(f"/subscribe/unsubscribe?token={token}")
        assert page.status_code == 200 and "Unsubscribe?" in page.text
        done = client.post("/subscribe/unsubscribe", data={"token": token})
        assert "Unsubscribed" in done.text
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM webhook_subscribers").fetchone()["n"] == 0


# ── outage ping ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_outage_ping_posted_then_deleted_on_recovery(monkeypatch):
    monkeypatch.setenv("ALERT_DISCORD_WEBHOOK_URL", "https://discord.test/webhook")
    monkeypatch.setenv("ALERT_OUTAGE_MENTION", "<@&424242>")
    reset_settings()
    _insert_probe("Public Site", "down")  # critical service → overall "outage"

    with respx.mock(assert_all_called=False) as router:
        post = router.post("https://discord.test/webhook").mock(
            return_value=httpx.Response(200, json={"id": "77"}))
        patch = router.patch(url__regex=r"https://discord\.test/webhook/messages/\d+").mock(
            return_value=httpx.Response(200, json={"id": "77"}))
        delete = router.delete(url__regex=r"https://discord\.test/webhook/messages/\d+").mock(
            return_value=httpx.Response(204))
        async with httpx.AsyncClient() as client:
            a = Alerter(get_settings(), client)
            await a.evaluate([])
            # board create + mention ping = two POSTs, one carries the mention
            assert post.call_count == 2
            payloads = [json.loads(c.request.content) for c in post.calls]
            assert any("<@&424242>" in (p.get("content") or "") for p in payloads)
            assert db.kv_get("alert_outage_ping_msg_id") == "77"

            _insert_probe("Public Site", "operational")   # recovery
            await a.evaluate([])
            assert delete.called                           # ping cleaned up
            assert post.call_count == 2                    # no new ping
    assert db.kv_get("alert_last_overall") == "operational"


@pytest.mark.asyncio
async def test_no_outage_ping_without_mention_config(monkeypatch):
    monkeypatch.setenv("ALERT_DISCORD_WEBHOOK_URL", "https://discord.test/webhook")
    monkeypatch.setenv("ALERT_OUTAGE_MENTION", "")
    reset_settings()
    _insert_probe("Public Site", "down")

    with respx.mock(assert_all_called=False) as router:
        post = router.post("https://discord.test/webhook").mock(
            return_value=httpx.Response(200, json={"id": "5"}))
        async with httpx.AsyncClient() as client:
            a = Alerter(get_settings(), client)
            await a.evaluate([])
    # only the board itself — no mention ping
    assert post.call_count == 1
    payloads = [json.loads(c.request.content) for c in post.calls]
    assert all("content" not in p for p in payloads)
