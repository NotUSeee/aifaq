"""Reports sent TO the status service: the platform's own health, and
website checks made from other places.

What these are for:
  * when the website is down the page can still say whether the bot works,
    because the platform's checker reports to us directly
  * one place failing to reach the website (ours included) is that place's
    route, not an outage: the site is down when MOST places cannot reach it
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from status_service import db, ingest
from status_service.aggregator import build_components, incident_events, latest_per_service, overall_status
from status_service.config import reset_settings
from status_service.main import app
from status_service.probes import ProbeResult
from status_service.scheduler import site_consensus
from status_service.snapshot import build_snapshot
from tests.test_measurement import BASE, CONTROLS, DISCORD, DISCORD_OK, PLATFORM, SHARDS, _rows, _statuses, sched  # noqa: F401

PLATFORM_SECRET = "platform-secret-" + "p" * 32
VANTAGE_SECRET = "vantage-secret-" + "v" * 32
ADMIN_SECRET = "test-secret-deadbeef"                      # conftest's ADMIN_HMAC_SECRET


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("INGEST_PLATFORM_SECRET", PLATFORM_SECRET)
    monkeypatch.setenv("INGEST_VANTAGE_SECRET", VANTAGE_SECRET)
    reset_settings()
    yield
    reset_settings()


def _post(client, path: str, payload: dict, secret: str, ts: int | None = None, raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode()
    return client.post(path, content=body, headers={"Content-Type": "application/json", **ingest.sign(secret, body, ts)})


def _platform_payload(**overrides) -> dict:
    current = {name: dict(entry) for name, entry in PLATFORM["current"].items()}
    current.update(overrides)
    return {"status": {"current": current}, "shards": SHARDS}


def _push_platform(payload: dict | None = None, ts: int | None = None) -> None:
    ingest.store_platform_report(payload or _platform_payload(), ts if ts is not None else int(time.time()))


def _vantage(name: str, status: str, label: str | None = None) -> None:
    ingest.store_vantage_report({"vantage": name, "status": status, "label": label or name,
                                 "http_status": 200 if status == "operational" else None,
                                 "response_ms": 150, "error": None if status == "operational" else "timeout"},
                                int(time.time()))


def _age(key: str, seconds: float) -> None:
    """Make a held report look `seconds` old."""
    from datetime import datetime, timedelta, timezone
    held = json.loads(db.kv_get(key))
    held["received_at"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    db.kv_set(key, json.dumps(held))


def _site(respx_mock, status: int, status_api: int = 200):
    respx_mock.get(f"{BASE}/readiness").mock(
        return_value=httpx.Response(status, json={"ok": status == 200, "db": "ok", "redis": "ok"}))
    respx_mock.get(f"{BASE}/status/api").mock(
        return_value=httpx.Response(status_api, json=PLATFORM if status_api == 200 else {"error": "boom"}))
    respx_mock.get(f"{BASE}/status/api/shards").mock(return_value=httpx.Response(200, json=SHARDS))
    respx_mock.get(DISCORD).mock(return_value=httpx.Response(200, json=DISCORD_OK))
    controls = [respx_mock.get(url).mock(return_value=httpx.Response(204)) for url in CONTROLS]
    return controls


def _component(key: str) -> dict:
    return next(c for c in build_components(latest_per_service()) if c["key"] == key)


# ── who may send ──────────────────────────────────────────────────────────

def test_the_routes_do_not_exist_until_a_secret_is_set(monkeypatch):
    monkeypatch.setenv("INGEST_PLATFORM_SECRET", "")
    monkeypatch.setenv("INGEST_VANTAGE_SECRET", "too-short")
    reset_settings()
    with TestClient(app) as client:
        assert _post(client, "/ingest/platform", _platform_payload(), PLATFORM_SECRET).status_code == 404
        assert _post(client, "/ingest/vantage", {"vantage": "cf", "status": "operational"}, "too-short").status_code == 404
    assert ingest.fresh_platform_report() is None


def test_only_a_correctly_signed_recent_report_is_accepted():
    payload = _platform_payload()
    with TestClient(app) as client:
        assert client.post("/ingest/platform", json=payload).status_code == 401                       # unsigned
        assert _post(client, "/ingest/platform", payload, "x" * 40).status_code == 401                 # wrong secret
        assert _post(client, "/ingest/platform", payload, PLATFORM_SECRET, ts=int(time.time()) - 600).status_code == 401
        body = json.dumps(payload).encode()
        tampered = client.post("/ingest/platform", content=body.replace(b"operational", b"down       ", 1),
                               headers=ingest.sign(PLATFORM_SECRET, body))
        assert tampered.status_code == 401                                                             # body changed after signing
        assert ingest.fresh_platform_report() is None
        ok = _post(client, "/ingest/platform", payload, PLATFORM_SECRET)
        assert ok.status_code == 200 and ok.json() == {"ok": True}
    assert ingest.fresh_platform_report()["status"]["current"]["Bot"]["status"] == "operational"


def test_each_sender_can_only_speak_for_itself():
    """The vantage secret lives in someone else's cloud and the platform
    secret on the platform. Neither may forge the other, and neither is the
    admin secret."""
    with TestClient(app) as client:
        assert _post(client, "/ingest/platform", _platform_payload(), VANTAGE_SECRET).status_code == 401
        assert _post(client, "/ingest/platform", _platform_payload(), ADMIN_SECRET).status_code == 401
        assert _post(client, "/ingest/vantage", {"vantage": "cf", "status": "down"}, PLATFORM_SECRET).status_code == 401
        announce = {"type": "incident", "severity": "critical", "title": "x", "body": "y"}
        assert _post(client, "/admin/announce", announce, PLATFORM_SECRET).status_code == 401
        assert _post(client, "/admin/announce", announce, VANTAGE_SECRET).status_code == 401


def test_bad_reports_are_refused_and_an_older_one_never_replaces_a_newer_one():
    now = int(time.time())
    with TestClient(app) as client:
        assert _post(client, "/ingest/platform", {"shards": {}}, PLATFORM_SECRET).status_code == 422
        assert _post(client, "/ingest/platform", {}, PLATFORM_SECRET, raw=b"not json").status_code == 422
        assert _post(client, "/ingest/platform", {}, PLATFORM_SECRET, raw=b"x" * (ingest.MAX_BODY_BYTES + 1)).status_code == 413
        assert _post(client, "/ingest/vantage", {"vantage": "Bad Name!", "status": "down"}, VANTAGE_SECRET).status_code == 422
        assert _post(client, "/ingest/vantage", {"vantage": "cf", "status": "degraded"}, VANTAGE_SECRET).status_code == 422
        assert _post(client, "/ingest/platform", _platform_payload(), PLATFORM_SECRET, ts=now).status_code == 200
        late = _platform_payload(Bot={"status": "down", "response_ms": 1})
        assert _post(client, "/ingest/platform", late, PLATFORM_SECRET, ts=now - 30).status_code == 409   # signed earlier
    assert ingest.fresh_platform_report()["status"]["current"]["Bot"]["status"] == "operational"


def test_the_number_of_vantage_points_is_capped():
    for i in range(ingest.MAX_VANTAGE_POINTS):
        _vantage(f"place-{i}", "operational")
    with pytest.raises(ingest.Rejected) as exc:
        _vantage("one-too-many", "operational")
    assert exc.value.status == 409
    _vantage("place-0", "down")                              # an existing one can still report
    assert len(ingest.vantage_reports()) == ingest.MAX_VANTAGE_POINTS


# ── the platform's report, when the website is down ───────────────────────

@pytest.mark.asyncio
async def test_with_the_website_down_the_other_systems_keep_their_real_state(sched, respx_mock):  # noqa: F811
    _site(respx_mock, 503)
    _push_platform()
    await sched._cycle()
    statuses = _statuses()
    assert statuses["Public Site"] == "down"
    for name in ("Gateway", "Bot", "Bot Worker", "Plugin Runner", "Orchestrator", "Analytics", "Database", "Cache"):
        assert statuses[name] == "operational", name
    assert _rows("Bot")[-1]["source"] == "push"
    assert _component("bot")["status"] == "operational" and _component("commands")["status"] == "operational"
    snap = build_snapshot()
    assert snap["headline"] == "Major outage"                # the website is still down, and that is still major
    assert snap["detail"] == ("Affected: Website and dashboard. The website cannot be reached. "
                              "The other systems are still reporting to us directly.")
    assert snap["numbers"]["shards"]["online"] == 1          # shard health came with the report too


@pytest.mark.asyncio
async def test_a_real_failure_in_the_direct_report_is_shown_as_one(sched, respx_mock):  # noqa: F811
    _site(respx_mock, 503)
    _push_platform(_platform_payload(Gateway={"status": "down", "response_ms": 5}))
    await sched._cycle()
    assert _statuses()["Gateway"] == "down"
    assert _component("bot")["status"] == "down"


@pytest.mark.asyncio
async def test_a_stale_direct_report_is_not_used(sched, respx_mock):  # noqa: F811
    _site(respx_mock, 503)
    _push_platform()
    _age(ingest.PLATFORM_KEY, ingest.PLATFORM_FRESH_SECONDS + 30)
    await sched._cycle()
    assert _statuses()["Bot"] == "unknown"
    assert _rows("Bot")[-1]["error"] == "not measured: website unreachable"
    assert "cannot be checked until it is back" in build_snapshot()["detail"]


@pytest.mark.asyncio
async def test_with_the_website_up_the_report_is_fetched_as_before(sched, respx_mock):  # noqa: F811
    _site(respx_mock, 200)
    _push_platform(_platform_payload(Bot={"status": "down", "response_ms": 1}))   # a different story, to tell them apart
    await sched._cycle()
    assert _statuses()["Bot"] == "operational" and _rows("Bot")[-1]["source"] == "proxy"


@pytest.mark.asyncio
async def test_if_the_website_answers_but_its_status_endpoint_does_not_the_direct_report_is_used(sched, respx_mock):  # noqa: F811
    _site(respx_mock, 200, status_api=500)
    _push_platform()
    await sched._cycle()
    assert _statuses()["Bot"] == "operational" and _rows("Bot")[-1]["source"] == "push"


@pytest.mark.asyncio
async def test_a_service_missing_from_the_direct_report_is_no_data_not_a_stale_state(sched, respx_mock):  # noqa: F811
    _site(respx_mock, 200)
    await sched._cycle()                                     # Sandbox seen operational
    _site(respx_mock, 503)
    payload = _platform_payload()
    del payload["status"]["current"]["Sandbox"]
    _push_platform(payload)
    await sched._cycle()
    assert _statuses()["Sandbox"] == "unknown"


@pytest.mark.asyncio
async def test_when_our_own_connection_is_down_a_held_report_is_not_passed_off_as_current(sched, respx_mock):  # noqa: F811
    """A report that arrived a minute before our line went down is still
    "fresh" by the clock. It must not keep the page on "operational" while
    nothing can reach us any more."""
    _site(respx_mock, 503)
    for url in CONTROLS:
        respx_mock.get(url).mock(side_effect=httpx.ConnectError("no route"))
    _push_platform()
    _age(ingest.PLATFORM_KEY, 60)
    await sched._cycle()
    assert _statuses()["Bot"] == "unknown"
    assert _rows("Bot")[-1]["error"] == "monitor offline"
    assert overall_status(latest_per_service()) == "unknown"


# ── the website, as seen from several places ──────────────────────────────

def _local(status: str) -> ProbeResult:
    return ProbeResult(service_name="Public Site", status=status, response_ms=170 if status != "down" else 3004,
                       error=None if status != "down" else "timeout", source="external")


@pytest.mark.parametrize("local, others, verdict", [
    ("operational", [], "operational"),
    ("down", [], "down"),                                     # nobody else looked: ours stands (and is then self-checked)
    ("down", ["operational", "operational"], "operational"),  # only our route failed
    ("down", ["operational"], "operational"),                 # one each: not most
    ("operational", ["down"], "operational"),
    ("operational", ["down", "operational"], "operational"),
    ("operational", ["down", "down"], "down"),                # we can reach it, most cannot
    ("down", ["down", "operational"], "down"),
    ("down", ["down", "down"], "down"),
    ("down", ["down"], "down"),
])
def test_the_website_is_down_when_most_places_cannot_reach_it(local, others, verdict):
    reports = [{"vantage": f"p{i}", "status": s, "error": "timeout" if s == "down" else None} for i, s in enumerate(others)]
    result, note = site_consensus(_local(local), reports)
    assert result.status == verdict
    assert result.service_name == "Public Site" and result.source == "external"
    if others and "down" in [local, *others]:
        assert note and ("unreachable from" in note or "reached from" in note)


@pytest.mark.asyncio
async def test_our_own_failed_route_is_not_an_outage_when_others_reach_the_site(sched, respx_mock):  # noqa: F811
    controls = _site(respx_mock, 503)
    _vantage("cloudflare", "operational", "Cloudflare")
    _vantage("google", "operational", "Google")
    _push_platform()
    await sched._cycle()
    assert _statuses()["Public Site"] == "operational"
    assert _rows("Public Site")[-1]["error"] is None
    assert overall_status(latest_per_service()) == "operational"
    assert incident_events(days=7) == []
    assert all(c.call_count == 0 for c in controls)           # no need to test our connection: their reports reached us
    assert _statuses()["Bot"] == "operational"                # and the platform's direct report filled in the rest


@pytest.mark.asyncio
async def test_without_a_direct_report_what_we_could_not_fetch_is_no_data(sched, respx_mock):  # noqa: F811
    _site(respx_mock, 503)
    _vantage("cloudflare", "operational")
    _vantage("google", "operational")
    await sched._cycle()
    assert _statuses()["Public Site"] == "operational"
    assert _statuses()["Bot"] == "unknown"                    # we could not read it, and nobody sent it


@pytest.mark.asyncio
async def test_an_outage_seen_from_everywhere_is_confirmed_without_testing_our_own_line(sched, respx_mock):  # noqa: F811
    controls = _site(respx_mock, 503)
    _vantage("cloudflare", "down")
    _vantage("google", "down")
    await sched._cycle()
    assert _statuses()["Public Site"] == "down"
    assert all(c.call_count == 0 for c in controls)


@pytest.mark.asyncio
async def test_most_places_failing_is_an_outage_even_when_we_can_reach_the_site(sched, respx_mock):  # noqa: F811
    _site(respx_mock, 200)
    _vantage("cloudflare", "down")
    _vantage("google", "down")
    await sched._cycle()
    assert _statuses()["Public Site"] == "down"
    assert _statuses()["Bot"] == "operational"                # we could still read the platform ourselves


@pytest.mark.asyncio
async def test_reports_that_have_gone_quiet_do_not_vote(sched, respx_mock):  # noqa: F811
    controls = _site(respx_mock, 503)
    _vantage("cloudflare", "operational")
    _age(ingest.VANTAGE_PREFIX + "cloudflare", ingest.VANTAGE_FRESH_SECONDS + 20)
    await sched._cycle()
    assert _statuses()["Public Site"] == "down"               # our own check stands
    assert sum(c.call_count for c in controls) >= 1           # and our own connection was tested, as before


# ── what the page says about it ───────────────────────────────────────────

def test_the_page_names_the_places_only_while_they_are_reporting():
    from status_service.aggregator import roll_up_after_probe
    with db.connect() as conn:
        conn.execute("INSERT INTO probe_results(service_name,status,response_ms,source) VALUES ('Public Site','operational',170,'external')")
        conn.execute("INSERT INTO probe_results(service_name,status,response_ms,source) VALUES ('Dashboard','operational',5,'proxy')")
    roll_up_after_probe()
    with TestClient(app) as client:
        html = client.get("/").text
        assert "A monitor outside YourBot's servers loads yourbot.gg every minute." in html
        assert "Checked from outside" in html and "also sends them to this page directly" not in html
        assert "places" not in html.split('id="c-website"')[1].split("</li>")[0]     # one checker is not "several places"
        assert client.get("/api").json()["meta"]["platform_reports_directly"] is False

        ingest.store_own_site_check("operational", 170)
        _vantage("cloudflare", "operational", "Cloudflare")
        _vantage("google-us-east", "down", "Google, US East")
        _push_platform()
        html = client.get("/").text
        assert ("yourbot.gg is loaded every minute from 3 places on separate networks: "
                "our own monitor, Cloudflare, Google, US East.") in html
        assert "Checked from 3 places" in html
        assert "Reached from 2 of 3 places just now: our own monitor, Cloudflare, Google, US East." in html
        assert "also sends them to this page directly" in html
        meta = client.get("/api").json()["meta"]
        assert [(p["name"], p["status"]) for p in meta["places"]] == [
            ("home", "operational"), ("cloudflare", "operational"), ("google-us-east", "down")]
        assert meta["platform_reports_directly"] is True

        # a place that stopped reporting ten minutes ago is no longer claimed
        _age(ingest.VANTAGE_PREFIX + "google-us-east", 700)
        html = client.get("/").text
        assert "from 2 places on separate networks: our own monitor, Cloudflare." in html
