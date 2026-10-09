"""Each shard is its own check once the shared bot runs on more than one.

The platform's Gateway check passes while ANY shard is alive. With several
shards that is no longer the whole bot: one dead shard leaves every server
on it without the bot, and the page used to go on saying "All systems
operational".
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from status_service import db
from status_service.aggregator import (
    build_components,
    incident_events,
    latest_per_service,
    overall_status,
    roll_up_after_probe,
)
from status_service.alerter import Alerter
from status_service.components import group_of, is_shard, member_names, GROUP_BY_KEY
from status_service.config import get_settings
from status_service.main import app
from status_service.scheduler import shard_results
from status_service.snapshot import build_snapshot
from tests.test_measurement import BASE, DISCORD, DISCORD_OK, PLATFORM, _rows, _statuses, sched  # noqa: F401


def _shards(*states: str) -> dict:
    return {"clusters": [{"instance_id": "master", "shards": [
        {"shard_id": i, "status": st, "latency_ms": None if st == "down" else 40 + i, "guilds": 300 + i}
        for i, st in enumerate(states)]}]}


def _platform(respx_mock, shards: dict, site: int = 200):
    respx_mock.get(f"{BASE}/readiness").mock(
        return_value=httpx.Response(site, json={"ok": site == 200, "db": "ok", "redis": "ok"}))
    respx_mock.get(f"{BASE}/status/api").mock(return_value=httpx.Response(200, json=PLATFORM))
    respx_mock.get(f"{BASE}/status/api/shards").mock(return_value=httpx.Response(200, json=shards))
    respx_mock.get(DISCORD).mock(return_value=httpx.Response(200, json=DISCORD_OK))


def _component(key: str) -> dict:
    return next(c for c in build_components(latest_per_service()) if c["key"] == key)


# ── what gets measured ────────────────────────────────────────────────────

def test_a_single_shard_adds_no_check_of_its_own():
    """With one shard the Gateway check already is that shard."""
    assert shard_results(_shards("operational")) == []
    assert shard_results({"clusters": []}) == []


def test_several_shards_become_one_check_each():
    results = shard_results(_shards("operational", "down", "degraded", "weird"))
    assert [(r.service_name, r.status, r.response_ms) for r in results] == [
        ("Shard 0", "operational", 40), ("Shard 1", "down", None), ("Shard 2", "degraded", 42),
        ("Shard 3", "unknown", 43),            # a state we do not know is no data, never a guess
    ]
    assert all(r.source == "proxy" for r in results)


def test_a_shard_reported_twice_keeps_the_worse_report():
    payload = {"clusters": [
        {"shards": [{"shard_id": 0, "status": "operational"}, {"shard_id": 1, "status": "operational"}]},
        {"shards": [{"shard_id": 1, "status": "down"}]},
    ]}
    assert [(r.service_name, r.status) for r in shard_results(payload)] == [("Shard 0", "operational"), ("Shard 1", "down")]


def test_shards_belong_to_the_bot_component_in_number_order():
    names = {"Gateway", "Shard 10", "Shard 2", "Shard 0", "Dashboard"}
    assert all(group_of(n) is GROUP_BY_KEY["bot"] for n in names if is_shard(n))
    assert member_names(GROUP_BY_KEY["bot"], names) == ["Gateway", "Shard 0", "Shard 2", "Shard 10"]
    assert not is_shard("Shard x") and not is_shard("Sharding")


@pytest.mark.asyncio
async def test_one_dead_shard_is_a_partial_outage_not_all_clear(sched, respx_mock):  # noqa: F811
    _platform(respx_mock, _shards("operational", "down", "operational", "operational"))
    await sched._cycle()
    statuses = _statuses()
    assert statuses["Gateway"] == "operational"            # the platform's own check still passes
    assert statuses["Shard 1"] == "down" and statuses["Shard 0"] == "operational"
    assert overall_status(latest_per_service()) == "partial_outage"
    bot = _component("bot")
    assert bot["status"] == "partial"
    assert [k["name"] for k in bot["checks"]] == ["Gateway", "Shard 0", "Shard 1", "Shard 2", "Shard 3"]
    snap = build_snapshot()
    assert snap["headline"] == "Partial outage"
    assert snap["detail"] == "Affected: YourBot in Discord."
    # and the other components are untouched
    assert _component("commands")["status"] == "operational"


@pytest.mark.asyncio
async def test_a_slow_shard_is_degraded_performance(sched, respx_mock):  # noqa: F811
    _platform(respx_mock, _shards("operational", "degraded"))
    await sched._cycle()
    assert overall_status(latest_per_service()) == "degraded"
    assert _component("bot")["status"] == "degraded"


@pytest.mark.asyncio
async def test_every_shard_down_with_the_gateway_is_a_major_outage(sched, respx_mock):  # noqa: F811
    platform = {"current": {**PLATFORM["current"], "Gateway": {"status": "down", "response_ms": 5}}}
    respx_mock.get(f"{BASE}/readiness").mock(
        return_value=httpx.Response(200, json={"ok": True, "db": "ok", "redis": "ok"}))
    respx_mock.get(f"{BASE}/status/api").mock(return_value=httpx.Response(200, json=platform))
    respx_mock.get(f"{BASE}/status/api/shards").mock(return_value=httpx.Response(200, json=_shards("down", "down")))
    respx_mock.get(DISCORD).mock(return_value=httpx.Response(200, json=DISCORD_OK))
    await sched._cycle()
    assert overall_status(latest_per_service()) == "outage"
    assert _component("bot")["status"] == "down"


@pytest.mark.asyncio
async def test_shards_go_to_no_data_with_everything_else_when_the_platform_cannot_be_read(sched, respx_mock):  # noqa: F811
    _platform(respx_mock, _shards("operational", "operational"))
    await sched._cycle()
    assert _statuses()["Shard 1"] == "operational"
    respx_mock.get(f"{BASE}/readiness").mock(return_value=httpx.Response(503, json={"ok": False}))
    for url in ("https://control-a.example/ok", "https://control-b.example/ok"):
        respx_mock.get(url).mock(return_value=httpx.Response(204))
    await sched._cycle()
    assert [(r["status"], r["error"]) for r in _rows("Shard 1")] == [
        ("operational", None), ("unknown", "not measured: website unreachable")]
    assert _component("bot")["status"] == "unknown"


@pytest.mark.asyncio
async def test_back_on_one_shard_no_new_shard_checks_are_written(sched, respx_mock):  # noqa: F811
    _platform(respx_mock, _shards("operational", "operational"))
    await sched._cycle()
    _platform(respx_mock, _shards("operational"))
    await sched._cycle()
    assert len(_rows("Shard 0")) == 1 and len(_rows("Shard 1")) == 1


# ── what it costs and how it is shown ─────────────────────────────────────

def _day(name: str, failed: int, total: int = 1440) -> None:
    # yesterday: today's figures are always worked out live from the checks themselves
    day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_uptime(service_name, day, uptime_pct, total_checks, failed_checks) "
            "VALUES (?,?,?,?,?)", (name, day, round((total - failed) / total * 100.0, 3), total, failed))


def _probe(name: str, status: str, at: datetime | None = None) -> None:
    ts = (at or datetime.now(timezone.utc)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with db.connect() as conn:
        conn.execute("INSERT INTO probe_results(service_name,status,source,checked_at) VALUES (?,?,'proxy',?)",
                     (name, status, ts))


def test_the_component_takes_the_uptime_of_its_worst_shard():
    for name in ("Gateway", "Shard 0", "Shard 1"):
        _probe(name, "operational")
    _day("Gateway", 0)
    _day("Shard 0", 0)
    _day("Shard 1", 36)                                     # 36 failed minutes on one shard
    bot = _component("bot")
    assert bot["uptime"]["30d"]["failed"] == 36 and bot["uptime"]["30d"]["pct"] == 97.5
    snap = build_snapshot()
    comp = next(c for c in snap["components"] if c["key"] == "bot")
    assert comp["cells"][-2]["cls"] == "major"              # yesterday's cell on the component bar
    by_name = {k["name"]: k for k in comp["checks"]}
    assert by_name["Shard 1"]["cells"][-2]["cls"] == "major" and by_name["Shard 0"]["cells"][-2]["cls"] == "ok"
    assert by_name["Gateway"]["cells"][-2]["cls"] == "ok"


def test_a_dead_shard_is_listed_as_an_incident_under_the_bot():
    start = datetime.now(timezone.utc) - timedelta(minutes=9)
    for minute in range(0, 6):
        _probe("Gateway", "operational", start + timedelta(minutes=minute))
        _probe("Shard 0", "operational", start + timedelta(minutes=minute))
        _probe("Shard 1", "down", start + timedelta(minutes=minute))
        roll_up_after_probe()
    events = incident_events(days=7)
    assert len(events) == 1 and not events[0]["resolved"]
    assert events[0]["services"] == ["Shard 1"]
    assert [g["key"] for g in events[0]["groups"]] == ["bot"]
    assert "YourBot in Discord" in events[0]["title"]


def test_the_page_lists_each_shard_with_its_own_bar():
    for name in ("Public Site", "Gateway", "Shard 0", "Shard 1", "Shard 2"):
        _probe(name, "down" if name == "Shard 2" else "operational")
    roll_up_after_probe()
    with TestClient(app) as client:
        html = client.get("/").text
        assert "Partial outage" in html
        block = re.search(r'<li class="comp comp-partial" id="c-bot".*?</li>', html, flags=re.S).group(0)
        assert re.findall(r'<span class="check-name">([^<]+)</span>', block) == ["Gateway", "Shard 0", "Shard 1", "Shard 2"]
        assert block.count('class="daybar daybar-sm"') == 4          # Gateway and each shard
        assert "Each server is served by one shard." in block
        assert client.get("/badge/shard-2.svg").status_code == 200 and "down" in client.get("/badge/shard-2.svg").text
        api = client.get("/api").json()
    bot = next(c for c in api["components"] if c["key"] == "bot")
    assert bot["status"] == "partial" and [k["name"] for k in bot["checks"]][1:] == ["Shard 0", "Shard 1", "Shard 2"]


def test_the_discord_status_board_lists_shards_under_the_bot():
    for name in ("Public Site", "Gateway", "Shard 0", "Shard 1"):
        _probe(name, "down" if name == "Shard 1" else "operational")
    currents = latest_per_service()
    embed = Alerter(get_settings(), None)._build_board_embed(currents, overall_status(currents))
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert "Shard 0" in fields["YourBot in Discord"] and "Shard 1" in fields["YourBot in Discord"]
    assert "Other" not in fields
