"""How a check becomes downtime (or does not).

These pin the rules the rework exists for:
  * one failed request is not an outage: it is retried
  * when the monitor's own connection is down, nothing is recorded as downtime
  * a website failure is never copied onto the services behind it
"""

from __future__ import annotations

import ssl as _ssl

import httpx
import pytest

from status_service import db
from status_service.aggregator import incident_events, latest_per_service, overall_status, uptime_windows
from status_service.config import get_settings
from status_service.probes import ProbeResult
from status_service.probes import ssl as ssl_probe
from status_service.probes.discord_status import probe_discord_status, summarize
from status_service.probes.http import probe_readiness
from status_service.probes.monitor import monitor_online, parse_control_urls
from status_service.scheduler import MONITOR_SERVICE, Scheduler, merge_cycle_results

BASE = "https://test.example.com"
CONTROLS = ["https://control-a.example/ok", "https://control-b.example/ok"]
DISCORD = "https://discordstatus.example/summary.json"

PLATFORM = {
    "current": {
        "Dashboard": {"status": "operational", "response_ms": 3},
        "Gateway": {"status": "operational", "response_ms": 9},
        "Bot": {"status": "operational", "response_ms": 7},
        "Bot Worker": {"status": "operational", "response_ms": 4},
        "Plugin Runner": {"status": "operational", "response_ms": 30},
        "Orchestrator": {"status": "operational", "response_ms": 28},
        "Analytics": {"status": "operational", "response_ms": 4},
        "Sandbox": {"status": "operational", "response_ms": 90},
        "Database": {"status": "operational", "response_ms": 8},
        "Cache": {"status": "operational", "response_ms": 3},
    },
}
SHARDS = {"clusters": [{"instance_id": "master", "shards": [
    {"shard_id": 0, "status": "operational", "latency_ms": 46, "guilds": 861}]}]}
DISCORD_OK = {"components": [{"name": "API", "status": "operational"},
                             {"name": "Gateway", "status": "operational"},
                             {"name": "Voice", "status": "major_outage"}], "incidents": []}


@pytest.fixture
def sched(monkeypatch):
    """A scheduler whose DNS and certificate probes never touch the network
    and whose retries do not sleep."""
    monkeypatch.setenv("PROBE_RETRY_DELAY_SECONDS", "0")
    monkeypatch.setenv("MONITOR_CONTROL_URLS", ",".join(CONTROLS))
    monkeypatch.setenv("DISCORD_STATUS_URL", DISCORD)
    from status_service.config import reset_settings
    reset_settings()

    state = {"dns": "operational"}

    async def fake_dns(base_url, timeout=3.0, attempts=1, retry_delay=1.0):
        st = state["dns"]
        return ProbeResult(service_name="DNS", status=st, response_ms=12, source="dns",
                           error=None if st == "operational" else "resolve timeout")

    async def fake_ssl(base_url, warn_days, critical_days, timeout=5.0):
        return ProbeResult(service_name="SSL Certificate", status="operational", response_ms=140,
                           source="ssl", extra={"days_left": 60})

    monkeypatch.setattr("status_service.scheduler.probe_dns", fake_dns)
    monkeypatch.setattr("status_service.scheduler.probe_ssl", fake_ssl)
    s = Scheduler(get_settings())
    s.dns_state = state
    return s


def _healthy(respx_mock):
    respx_mock.get(f"{BASE}/readiness").mock(
        return_value=httpx.Response(200, json={"ok": True, "db": "ok", "redis": "ok"}))
    respx_mock.get(f"{BASE}/status/api").mock(return_value=httpx.Response(200, json=PLATFORM))
    respx_mock.get(f"{BASE}/status/api/shards").mock(return_value=httpx.Response(200, json=SHARDS))
    respx_mock.get(DISCORD).mock(return_value=httpx.Response(200, json=DISCORD_OK))


def _statuses() -> dict[str, str]:
    return {c.name: c.status for c in latest_per_service()}


def _rows(service: str) -> list:
    with db.connect() as conn:
        return conn.execute(
            "SELECT status, error, source FROM probe_results WHERE service_name=? ORDER BY id", (service,)
        ).fetchall()


# ── retries ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_readiness_retries_before_reporting_down(respx_mock):
    """One dropped request followed by a good one is a healthy site."""
    route = respx_mock.get("https://x/readiness").mock(side_effect=[
        httpx.ConnectError("connection reset"),
        httpx.Response(200, json={"ok": True, "db": "ok", "redis": "ok"}),
    ])
    async with httpx.AsyncClient() as client:
        result, body = await probe_readiness(client, "https://x", attempts=3, retry_delay=0)
    assert route.call_count == 2
    assert result.status == "operational"
    assert body["db"] == "ok"


@pytest.mark.asyncio
async def test_readiness_down_only_after_every_attempt_fails(respx_mock):
    route = respx_mock.get("https://x/readiness").mock(side_effect=httpx.ConnectTimeout("no route"))
    async with httpx.AsyncClient() as client:
        result, _ = await probe_readiness(client, "https://x", attempts=3, retry_delay=0)
    assert route.call_count == 3
    assert result.status == "down"
    assert result.extra["attempts"] == 3


@pytest.mark.asyncio
async def test_readiness_error_status_is_down(respx_mock):
    respx_mock.get("https://x/readiness").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        result, _ = await probe_readiness(client, "https://x", attempts=2, retry_delay=0)
    assert result.status == "down"
    assert result.http_status == 503


# ── the monitor's own connection ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_monitor_online_needs_only_one_control(respx_mock):
    respx_mock.get(CONTROLS[0]).mock(side_effect=httpx.ConnectError("down"))
    respx_mock.get(CONTROLS[1]).mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as client:
        assert await monitor_online(client, CONTROLS) is True


@pytest.mark.asyncio
async def test_monitor_offline_when_no_control_answers(respx_mock):
    for url in CONTROLS:
        respx_mock.get(url).mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as client:
        assert await monitor_online(client, CONTROLS) is False


@pytest.mark.asyncio
async def test_monitor_check_disabled_returns_none():
    async with httpx.AsyncClient() as client:
        assert await monitor_online(client, parse_control_urls(" , ")) is None


# ── a full cycle ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_healthy_cycle_writes_one_row_per_service(respx_mock, sched):
    _healthy(respx_mock)
    await sched._cycle()
    st = _statuses()
    assert st["Public Site"] == "operational"
    assert st["Bot"] == "operational" and st["Gateway"] == "operational"
    assert st["Discord"] == "operational"      # a Discord voice outage does not concern a bot
    assert overall_status(latest_per_service()) == "operational"
    # Database is reported by /readiness AND by the platform: stored once.
    assert len(_rows("Database")) == 1
    assert _rows("Database")[0]["source"] == "proxy"
    assert _rows(MONITOR_SERVICE)[0]["status"] == "operational"
    await sched.aclose()


@pytest.mark.asyncio
async def test_confirmed_website_outage_is_not_copied_onto_other_services(respx_mock, sched):
    """The website is unreachable and the monitor is online: the website is
    down. The services behind it were not measured, so they are "no data",
    get no incident, and keep their uptime."""
    _healthy(respx_mock)
    await sched._cycle()                       # a healthy minute first

    respx_mock.get(f"{BASE}/readiness").mock(side_effect=httpx.ReadTimeout("slow"))
    for url in CONTROLS:
        respx_mock.get(url).mock(return_value=httpx.Response(204))
    await sched._cycle()

    st = _statuses()
    assert st["Public Site"] == "down"
    for name in ("Bot", "Gateway", "Bot Worker", "Orchestrator", "Plugin Runner", "Database", "Cache"):
        assert st[name] == "unknown", name
    assert _rows("Bot")[-1]["error"].startswith("not measured")
    assert overall_status(latest_per_service()) == "outage"

    with db.connect() as conn:
        open_names = {r["service_name"] for r in conn.execute("SELECT service_name FROM incidents WHERE resolved=0")}
    assert open_names == {"Public Site"}

    windows = uptime_windows()
    assert windows["Bot"]["24h"]["pct"] == 100.0          # one completed check, it passed
    assert windows["Bot"]["24h"]["checks"] == 1           # the unmeasured minute is not a check
    assert windows["Public Site"]["24h"]["pct"] == 50.0
    await sched.aclose()


@pytest.mark.asyncio
async def test_every_reported_service_goes_to_no_data_from_its_first_report(respx_mock, sched):
    """Regression, found by watching a live outage: services outside the core
    list (WebSocket Broker, Image Service, the dev portal) kept showing their
    last "operational" during an outage until a cached list caught up, which
    left "Marketplace plugins: Operational" on a page saying nothing could be
    checked. They must flip in the same cycle, even one cycle after they
    first appeared."""
    extra = {"WebSocket Broker": {"status": "operational", "response_ms": 17},
             "Image Service": {"status": "operational", "response_ms": 70},
             "Dev Portal Runner": {"status": "operational", "response_ms": 28},
             "Some Future Service": {"status": "operational", "response_ms": 5}}
    _healthy(respx_mock)
    respx_mock.get(f"{BASE}/status/api").mock(
        return_value=httpx.Response(200, json={"current": {**PLATFORM["current"], **extra}}))
    await sched._cycle()                       # their first report ever
    assert _statuses()["WebSocket Broker"] == "operational"

    respx_mock.get(f"{BASE}/readiness").mock(return_value=httpx.Response(503))
    for url in CONTROLS:
        respx_mock.get(url).mock(return_value=httpx.Response(204))
    await sched._cycle()

    st = _statuses()
    for name in extra:
        assert st[name] == "unknown", name
    from status_service.aggregator import build_components
    comps = {c["key"]: c["status"] for c in build_components(latest_per_service())}
    assert comps["website"] == "down"
    for key in ("bot", "commands", "custom-bots", "plugins", "analytics", "images", "data", "developer-portal"):
        assert comps[key] == "unknown", key
    await sched.aclose()


@pytest.mark.asyncio
async def test_monitor_offline_records_no_downtime_at_all(respx_mock, sched):
    """The monitor cannot reach anything, the control endpoints included.
    That is the monitor's problem: nothing is down, nothing opens an
    incident, and no uptime figure moves."""
    _healthy(respx_mock)
    await sched._cycle()

    respx_mock.get(f"{BASE}/readiness").mock(side_effect=httpx.ConnectError("network unreachable"))
    for url in CONTROLS:
        respx_mock.get(url).mock(side_effect=httpx.ConnectError("network unreachable"))
    sched.dns_state["dns"] = "down"
    await sched._cycle()

    st = _statuses()
    assert st["Public Site"] == "unknown"
    assert st["DNS"] == "unknown"
    assert st["Bot"] == "unknown" and st["Discord"] == "unknown"
    assert _rows("Public Site")[-1]["error"] == "monitor offline"
    assert _rows(MONITOR_SERVICE)[-1]["status"] == "down"
    assert overall_status(latest_per_service()) == "unknown"

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()["n"] == 0
    windows = uptime_windows()
    assert windows["Public Site"]["24h"]["pct"] == 100.0
    assert windows["Public Site"]["24h"]["failed"] == 0
    assert incident_events(days=1) == []
    await sched.aclose()


@pytest.mark.asyncio
async def test_failed_lookup_is_ignored_when_the_site_answered(respx_mock, sched):
    """The site was just reached by name, so the domain resolves. A lookup
    that failed anyway is the monitor's resolver."""
    _healthy(respx_mock)
    sched.dns_state["dns"] = "down"
    await sched._cycle()
    assert _statuses()["DNS"] == "unknown"
    assert overall_status(latest_per_service()) == "operational"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()["n"] == 0
    await sched.aclose()


@pytest.mark.asyncio
async def test_unreadable_platform_report_is_no_data(respx_mock, sched):
    _healthy(respx_mock)
    respx_mock.get(f"{BASE}/status/api").mock(return_value=httpx.Response(503))
    await sched._cycle()
    st = _statuses()
    assert st["Public Site"] == "operational"
    assert st["Bot"] == "unknown"
    # Database still has the website's own /readiness reading.
    assert st["Database"] == "operational"
    await sched.aclose()


def test_merge_cycle_results_keeps_the_worse_verdict():
    a = ProbeResult(service_name="Database", status="operational", source="external")
    b = ProbeResult(service_name="Database", status="down", source="proxy")
    c = ProbeResult(service_name="Cache", status="operational", source="external", response_ms=170)
    d = ProbeResult(service_name="Cache", status="operational", source="proxy", response_ms=3)
    merged = {r.service_name: r for r in merge_cycle_results([a, c, b, d])}
    assert merged["Database"].status == "down"
    assert merged["Cache"].source == "proxy" and merged["Cache"].response_ms == 3


# ── certificate probe ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ssl_cannot_connect_is_no_data(monkeypatch):
    def boom(host, port, timeout):
        raise OSError("network unreachable")
    monkeypatch.setattr(ssl_probe, "_probe_blocking", boom)
    result = await ssl_probe.probe_ssl("https://x", warn_days=14, critical_days=3)
    assert result.status == "unknown"


@pytest.mark.asyncio
async def test_ssl_refused_handshake_is_down(monkeypatch):
    def boom(host, port, timeout):
        raise _ssl.SSLCertVerificationError("certificate has expired")
    monkeypatch.setattr(ssl_probe, "_probe_blocking", boom)
    result = await ssl_probe.probe_ssl("https://x", warn_days=14, critical_days=3)
    assert result.status == "down"


# ── Discord's own status ──────────────────────────────────────────────────

def test_discord_summary_only_looks_at_api_and_gateway():
    assert summarize(DISCORD_OK) == ("operational", None)


def test_discord_summary_reports_trouble_with_discords_words():
    body = {"components": [{"name": "API", "status": "partial_outage"},
                           {"name": "Gateway", "status": "operational"}],
            "incidents": [{"name": "Elevated API errors"}]}
    status, note = summarize(body)
    assert status == "degraded"
    assert "Discord's API is partly unavailable" in note
    assert "Elevated API errors" in note


def test_discord_summary_major_outage_is_down():
    status, _ = summarize({"components": [{"name": "Gateway", "status": "major_outage"},
                                          {"name": "API", "status": "operational"}]})
    assert status == "down"


@pytest.mark.asyncio
async def test_discord_status_unreadable_is_no_data_not_down(respx_mock):
    respx_mock.get(DISCORD).mock(side_effect=httpx.ConnectError("x"))
    async with httpx.AsyncClient() as client:
        result = await probe_discord_status(client, DISCORD)
    assert result.status == "unknown"


@pytest.mark.asyncio
async def test_discord_status_disabled_with_blank_url():
    async with httpx.AsyncClient() as client:
        assert await probe_discord_status(client, "") is None


@pytest.mark.asyncio
async def test_discord_trouble_never_moves_yourbots_verdict(respx_mock, sched):
    _healthy(respx_mock)
    respx_mock.get(DISCORD).mock(return_value=httpx.Response(200, json={
        "components": [{"name": "API", "status": "major_outage"}, {"name": "Gateway", "status": "major_outage"}],
        "incidents": [{"name": "Widespread outage"}]}))
    await sched._cycle()
    assert _statuses()["Discord"] == "down"
    assert overall_status(latest_per_service()) == "operational"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()["n"] == 0
    await sched.aclose()
