"""What the page says: uptime figures, component roll-ups, incident events,
and the public output (page, live fragment, API, feed, history, badges)."""

from __future__ import annotations

import gzip
import re
import xml.dom.minidom as minidom
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

from fastapi.testclient import TestClient

import status_service
from status_service import db
from status_service.aggregator import (
    build_components,
    daily_uptime_series,
    event_duration_text,
    headline_uptime,
    incident_events,
    latest_per_service,
    overall_status,
    roll_up_after_probe,
    uptime_windows,
)
from status_service.main import app
from status_service.snapshot import build_snapshot, fmt_pct


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def _probe(name: str, status: str, source: str = "proxy", at: datetime | None = None,
           error: str | None = None, ms: int | None = 20):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO probe_results(service_name,status,response_ms,http_status,error,source,checked_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, status, ms, None, error, source, _iso(at or NOW())),
        )


def _incident(name: str, start: datetime, minutes: int | None, cause: str | None = None) -> int:
    with db.connect() as conn:
        if minutes is None:
            cur = conn.execute("INSERT INTO incidents(service_name, started_at, resolved) VALUES (?,?,0)",
                               (name, _iso(start)))
        else:
            cur = conn.execute(
                "INSERT INTO incidents(service_name, started_at, ended_at, duration_min, resolved, cause, cause_at) "
                "VALUES (?,?,?,?,1,?,?)",
                (name, _iso(start), _iso(start + timedelta(minutes=minutes)), minutes, cause,
                 _iso(start + timedelta(hours=1)) if cause else None))
        return int(cur.lastrowid)


def _all_up():
    for name, source in (("Public Site", "external"), ("Dashboard", "proxy"), ("DNS", "dns"),
                         ("Gateway", "proxy"), ("Bot", "proxy"), ("Bot Worker", "proxy"),
                         ("Orchestrator", "proxy"), ("Plugin Runner", "proxy"), ("Analytics", "proxy"),
                         ("Database", "proxy"), ("Cache", "proxy")):
        _probe(name, "operational", source=source)


# ── uptime ────────────────────────────────────────────────────────────────

def test_uptime_ignores_checks_that_could_not_be_made():
    """Three passed, one failed, six "no data": that is 75%, not 90% and not
    30%. An unmade check is neither up nor down."""
    for _ in range(3):
        _probe("Bot", "operational")
    _probe("Bot", "down")
    for _ in range(6):
        _probe("Bot", "unknown", error="not measured: website unreachable")
    roll_up_after_probe()
    w = uptime_windows()["Bot"]
    assert w["24h"] == {"pct": 75.0, "checks": 4, "failed": 1}
    with db.connect() as conn:
        row = conn.execute("SELECT total_checks, failed_checks, uptime_pct FROM daily_uptime WHERE service_name='Bot'").fetchone()
    assert (row["total_checks"], row["failed_checks"], row["uptime_pct"]) == (4, 1, 75.0)


def test_a_day_with_only_no_data_gets_no_uptime_row():
    _probe("Bot", "unknown", error="monitor offline")
    roll_up_after_probe()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM daily_uptime").fetchone()["n"] == 0
    assert uptime_windows().get("Bot", {}).get("24h") is None


def test_uptime_windows_cover_each_period():
    today = NOW().date()
    with db.connect() as conn:
        for back, failed in ((0, 0), (3, 10), (20, 30), (60, 100)):
            conn.execute(
                "INSERT INTO daily_uptime(service_name, day, uptime_pct, total_checks, failed_checks) VALUES (?,?,?,?,?)",
                ("Gateway", (today - timedelta(days=back)).isoformat(), 0.0, 1000, failed))
    w = uptime_windows()["Gateway"]
    assert w["7d"]["failed"] == 10 and w["7d"]["checks"] == 2000
    assert w["30d"]["failed"] == 40 and w["30d"]["checks"] == 3000
    assert w["90d"]["failed"] == 140 and w["90d"]["checks"] == 4000
    assert w["30d"]["pct"] == round(2960 / 3000 * 100, 3)


def test_uptime_text_never_rounds_up_to_100():
    assert fmt_pct({"pct": 99.9993, "failed": 1}) == "99.99%"
    assert fmt_pct({"pct": 100.0, "failed": 0}) == "100%"
    assert fmt_pct({"pct": 99.5, "failed": 7}) == "99.50%"
    assert fmt_pct(None) == "–"


# ── components ────────────────────────────────────────────────────────────

def _component(key: str) -> dict:
    return next(c for c in build_components(latest_per_service()) if c["key"] == key)


def test_components_use_customer_language_and_keep_every_check():
    _all_up()
    comps = build_components(latest_per_service())
    names = [c["name"] for c in comps]
    assert names[:4] == ["Website and dashboard", "YourBot in Discord", "Commands and automations", "Custom bots"]
    checks = {k["name"] for c in comps for k in c["checks"]}
    # every platform component stays visible, down to the database and cache
    for name in ("Dashboard", "Gateway", "Bot", "Bot Worker", "Orchestrator", "Plugin Runner", "Database", "Cache"):
        assert name in checks


def test_website_down_is_an_outage_for_that_component_only():
    _all_up()
    _probe("Public Site", "down", source="external", error="timeout")
    for name in ("Dashboard", "Gateway", "Bot", "Bot Worker"):
        _probe(name, "unknown", error="not measured: website unreachable")
    assert _component("website")["status"] == "down"
    assert _component("bot")["status"] == "unknown"
    assert _component("commands")["status"] == "unknown"


def test_one_check_down_in_a_component_is_a_partial_outage():
    _all_up()
    _probe("Sandbox", "down")
    assert _component("plugins")["status"] == "partial"
    assert overall_status(latest_per_service()) == "partial_outage"


def test_certificate_warning_is_not_customer_facing_trouble():
    _all_up()
    _probe("SSL Certificate", "degraded", source="ssl", error="10 days to expiry")
    assert _component("website")["status"] == "operational"
    assert overall_status(latest_per_service()) == "operational"


def test_component_uptime_is_its_weakest_check_and_skips_diagnostics():
    today = NOW().date().isoformat()
    _all_up()
    with db.connect() as conn:
        for name, failed in (("Public Site", 2), ("Dashboard", 5), ("DNS", 50)):
            conn.execute(
                "INSERT OR REPLACE INTO daily_uptime(service_name, day, uptime_pct, total_checks, failed_checks) "
                "VALUES (?,?,?,?,?)", (name, today, 0.0, 1000, failed))
    web = _component("website")
    assert web["uptime"]["30d"]["failed"] == 5           # Dashboard, not the domain lookup
    assert web["uptime"]["30d"]["pct"] == 99.5


def test_headline_is_the_average_of_core_components_only():
    comps = [
        {"core": True, "uptime": {"24h": {"pct": 100.0}, "7d": {"pct": 99.0}, "30d": {"pct": 98.0}, "90d": None}},
        {"core": True, "uptime": {"24h": {"pct": 99.0}, "7d": {"pct": 100.0}, "30d": {"pct": 100.0}, "90d": None}},
        {"core": False, "uptime": {"24h": {"pct": 0.0}, "7d": {"pct": 0.0}, "30d": {"pct": 0.0}, "90d": None}},
    ]
    h = headline_uptime(comps)
    assert h == {"24h": 99.5, "7d": 99.5, "30d": 99.0, "90d": None}


def test_stale_outside_check_falls_back_to_no_data():
    """If the scheduler stops, the page must not keep showing whatever it
    last saw. This used to apply to platform-reported rows only."""
    _probe("Public Site", "operational", source="external", at=NOW() - timedelta(minutes=12))
    site = next(c for c in latest_per_service() if c.name == "Public Site")
    assert site.status == "unknown"


def test_timeline_reports_component_days():
    today = NOW().date()
    with db.connect() as conn:
        for name, failed in (("Plugin Runner", 0), ("Sandbox", 4)):
            conn.execute(
                "INSERT INTO daily_uptime(service_name, day, uptime_pct, total_checks, failed_checks) VALUES (?,?,?,?,?)",
                (name, (today - timedelta(days=2)).isoformat(), round((1440 - failed) / 14.4, 3), 1440, failed))
    data = daily_uptime_series(days=90)
    day = data["groups"]["plugins"][0]
    assert day["failed_checks"] == 4 and day["service"] == "Sandbox"


# ── incidents ─────────────────────────────────────────────────────────────

def test_one_event_for_everything_that_failed_together():
    start = NOW() - timedelta(hours=5)
    ids = [_incident(name, start + timedelta(milliseconds=i), 6)
           for i, name in enumerate(("Public Site", "Dashboard", "Sandbox"))]
    events = incident_events(days=7)
    assert len(events) == 1
    ev = events[0]
    assert ev["ids"] == sorted(ids) and ev["id"] == min(ids)
    assert ev["title"] == "Disruption: Website and dashboard, Marketplace plugins"
    assert ev["service_labels"] == ["Website", "Dashboard", "Plugin dashboards"]
    assert event_duration_text(ev) == "lasted 6 min"


def test_on_and_off_trouble_is_one_event_that_says_so():
    start = NOW() - timedelta(hours=8)
    _incident("Public Site", start, 4)
    _incident("Public Site", start + timedelta(minutes=12), 3)
    _incident("Public Site", start + timedelta(minutes=25), 5)
    events = incident_events(days=7)
    assert len(events) == 1
    ev = events[0]
    assert ev["duration_min"] == 30 and ev["down_min"] == 12 and ev["intermittent"]
    assert event_duration_text(ev) == "on and off for 30 min, 12 min of it down"


def test_separate_events_stay_separate():
    start = NOW() - timedelta(days=2)
    _incident("Gateway", start, 8)
    _incident("Image Service", start + timedelta(hours=3), 4)
    titles = [e["title"] for e in incident_events(days=7)]
    assert titles == ["Images disruption", "YourBot in Discord disruption"]   # newest first


def test_short_blips_are_not_listed_unless_explained():
    start = NOW() - timedelta(hours=3)
    _incident("Bot", start, 1)
    assert incident_events(days=7) == []
    _incident("Gateway", start + timedelta(hours=1), 1, cause="Planned restart.")
    assert [e["cause"] for e in incident_events(days=7)] == ["Planned restart."]


def test_ongoing_event_is_marked_open():
    _incident("Gateway", NOW() - timedelta(minutes=9), None)
    ev = incident_events(days=7)[0]
    assert not ev["resolved"] and ev["ended_at"] is None
    assert event_duration_text(ev).endswith("so far")


def test_no_data_does_not_close_an_incident_but_recovery_does():
    _probe("Gateway", "down", at=NOW() - timedelta(minutes=3))
    roll_up_after_probe()
    _probe("Gateway", "unknown", error="monitor offline", at=NOW() - timedelta(minutes=2))
    roll_up_after_probe()
    with db.connect() as conn:
        assert conn.execute("SELECT resolved FROM incidents").fetchone()["resolved"] == 0
    _probe("Gateway", "operational", at=NOW() - timedelta(minutes=1))
    roll_up_after_probe()
    with db.connect() as conn:
        assert conn.execute("SELECT resolved FROM incidents").fetchone()["resolved"] == 1


def test_incident_with_no_fresh_evidence_closes_at_last_confirmed_down():
    down_at = NOW() - timedelta(minutes=50)
    _probe("Gateway", "down", at=down_at)
    with db.connect() as conn:
        conn.execute("INSERT INTO incidents(service_name, started_at, resolved) VALUES ('Gateway', ?, 0)",
                     (_iso(down_at - timedelta(minutes=4)),))
    _probe("Gateway", "unknown", error="monitor offline")
    roll_up_after_probe()
    with db.connect() as conn:
        row = conn.execute("SELECT resolved, ended_at, duration_min FROM incidents").fetchone()
    assert row["resolved"] == 1
    assert row["ended_at"] == _iso(down_at)
    assert row["duration_min"] == 4


def test_no_data_never_opens_an_incident():
    _probe("Bot", "unknown", error="not measured: website unreachable")
    roll_up_after_probe()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()["n"] == 0


# ── the public output ─────────────────────────────────────────────────────

def _visible_text(html: str) -> str:
    html = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def test_page_shows_components_periods_and_how_it_measures():
    _all_up()
    roll_up_after_probe()
    with TestClient(app) as client:
        html = client.get("/").text
    assert "All systems operational" in html
    for name in ("Website and dashboard", "YourBot in Discord", "Commands and automations", "Custom bots",
                 "Marketplace plugins", "Data storage"):
        assert name in html
    # the three periods the page offers, per component and per check
    for label in ("24 hours", "7 days", "30 days"):
        assert label in html
    assert html.count('class="d d-') >= 90                  # a 90-day bar was drawn
    assert "How this page works" in html
    assert "No incidents in the last 7 days." in html
    assert "echarts" not in html                            # no chart library to download


def test_public_pages_carry_no_em_dashes():
    """House style for customer copy."""
    _all_up()
    start = NOW() - timedelta(hours=5)
    _incident("Public Site", start, 6, cause="A deploy went wrong and was rolled back.")
    with db.connect() as conn:
        conn.execute("INSERT INTO announcements(type,severity,title,body) VALUES "
                     "('maintenance','info','Database upgrade','Short pause expected.')")
    with TestClient(app) as client:
        for path in ("/", "/history", "/live"):
            text = _visible_text(client.get(path).text)
            assert "—" not in text, path
        assert "—" not in client.get("/feed.xml").text


def test_page_names_what_is_affected_during_an_outage():
    _all_up()
    _probe("Public Site", "down", source="external", error="timeout")
    for name in ("Dashboard", "Gateway", "Bot", "Bot Worker", "Orchestrator", "Plugin Runner", "Analytics",
                 "Database", "Cache"):
        _probe(name, "unknown", error="not measured: website unreachable")
    snap = build_snapshot()
    assert snap["overall"] == "outage" and snap["headline"] == "Major outage"
    assert snap["detail"].startswith("Affected: Website and dashboard.")
    assert "cannot be checked until it is back" in snap["detail"]
    with TestClient(app) as client:
        html = client.get("/").text
    assert "Not checked while the website is unreachable." in html


def test_page_explains_a_paused_monitor_instead_of_claiming_an_outage():
    _probe("Public Site", "unknown", source="external", error="monitor offline")
    _probe("Bot", "unknown", error="monitor offline")
    _probe("__monitor__", "down", source="monitor", error="monitor offline")
    snap = build_snapshot()
    assert snap["overall"] == "unknown" and snap["headline"] == "Status checks paused"
    assert "not a sign that YourBot is down" in snap["detail"]


def test_discord_trouble_is_shown_without_blaming_yourbot():
    _all_up()
    _probe("Discord", "degraded", source="discord_status",
           error="Discord's API is partly unavailable. Discord says: Elevated API errors")
    snap = build_snapshot()
    assert snap["overall"] == "operational"
    assert snap["discord_note"].startswith("Discord's API is partly unavailable")
    with TestClient(app) as client:
        html = client.get("/").text
    assert "Outside YourBot" in html and "Elevated API errors" in html
    assert "discordstatus.com" in html


def test_live_fragment_carries_every_live_region():
    _all_up()
    with TestClient(app) as client:
        r = client.get("/live")
    assert r.status_code == 200
    for region in ("banner", "notices", "uptime", "components", "thirdparty", "incidents", "numbers"):
        assert f'data-live="{region}"' in r.text
    assert "__AGE_SECONDS__" not in r.text
    assert re.search(r'data-age="\d+\.\d"', r.text)


def test_api_exposes_components_with_each_period():
    _all_up()
    roll_up_after_probe()
    with TestClient(app) as client:
        body = client.get("/api").json()
    assert body["overall"] == "operational" and body["headline"] == "All systems operational"
    web = next(c for c in body["components"] if c["key"] == "website")
    assert set(web["uptime"]) == {"24h", "7d", "30d", "90d"}
    assert web["uptime"]["24h"]["pct"] == 100.0
    assert {k["name"] for k in web["checks"]} >= {"Public Site", "Dashboard"}
    assert body["uptime"]["24h"] == 100.0
    assert "monitor_online" in body["meta"]
    # the original contract is intact
    assert isinstance(body["current"], list) and "service_order" in body


def test_feed_has_one_item_per_event():
    start = NOW() - timedelta(days=1)
    for i, name in enumerate(("Public Site", "Dashboard", "Gateway", "Bot", "Database")):
        _incident(name, start + timedelta(milliseconds=i), 6)
    with TestClient(app) as client:
        r = client.get("/feed.xml")
    doc = minidom.parseString(r.content)
    items = doc.getElementsByTagName("item")
    assert len(items) == 1
    title = items[0].getElementsByTagName("title")[0].firstChild.data
    assert title == "Resolved: Disruption across 4 components (6 min)"


def test_history_groups_by_event():
    start = NOW() - timedelta(days=2)
    first = _incident("Public Site", start, 6, cause="Router firmware update went sideways.")
    for i, name in enumerate(("Dashboard", "Sandbox")):
        _incident(name, start + timedelta(milliseconds=i + 1), 6)
    with TestClient(app) as client:
        html = client.get("/history").text
    assert html.count('class="event"') == 1
    assert f'id="incident-{first}"' in html
    assert "Router firmware update went sideways." in html
    assert "Why this happened" in html


def test_badges_for_checks_and_for_components():
    _all_up()
    _probe("Sandbox", "down")
    with TestClient(app) as client:
        assert "operational" in client.get("/badge/gateway.svg").text
        assert "partial outage" in client.get("/badge/plugins.svg").text
        assert client.get("/badge/custom-bots.svg").status_code == 200
        assert client.get("/badge/nope.svg").status_code == 404


# ── nothing failing is not the same as everything working ─────────────────

def test_all_systems_operational_is_only_said_when_every_everyday_system_reports():
    """Found live: the website answered, the platform's own checker had
    stopped, every service behind it showed "No data", and the headline
    still read "All systems operational"."""
    _probe("Public Site", "operational", source="external")
    for name in ("Dashboard", "Gateway", "Bot", "Bot Worker", "Orchestrator", "Plugin Runner", "Analytics"):
        _probe(name, "unknown", error="HTTP 500")
    snap = build_snapshot()
    assert snap["overall"] == "limited" and snap["headline"] == "Some systems are not reporting"
    assert snap["detail"].startswith("No data right now from: YourBot in Discord, Commands and automations, "
                                     "Custom bots, Marketplace plugins, Analytics")
    assert "Everything we can measure is working." in snap["detail"]
    with TestClient(app) as client:
        html = client.get("/").text
        assert "Some systems are not reporting" in html and "All systems operational" not in html
        assert 'class="overall overall-limited"' in html
        assert client.get("/api").json()["overall"] == "limited"
        assert "limited data" in client.get("/badge.svg").text


def test_a_failure_still_outranks_missing_data():
    _probe("Public Site", "operational", source="external")
    _probe("Gateway", "down")
    for name in ("Bot", "Bot Worker", "Analytics"):
        _probe(name, "unknown", error="HTTP 500")
    assert overall_status(latest_per_service()) == "outage"
    _probe("Gateway", "degraded")
    assert overall_status(latest_per_service()) == "degraded"


def test_missing_data_outside_the_everyday_systems_does_not_change_the_headline():
    """Developer tooling, the support assistant and Discord are reported, but
    they are not what "all systems" promises."""
    _all_up()
    _probe("Dev Portal Runner", "unknown", error="HTTP 500")
    _probe("Dev Portal Bot", "unknown", error="HTTP 500")
    _probe("FAQ Matcher", "unknown", error="HTTP 500")
    _probe("Discord", "unknown", source="discord_status", error="timeout")
    assert overall_status(latest_per_service()) == "operational"


def test_one_quiet_check_beside_a_working_one_does_not_change_the_headline():
    _all_up()
    _probe("Sandbox", "unknown", error="HTTP 500")         # Plugin Runner still reports
    _probe("DNS", "unknown", source="dns", error="lookup failed while the site answered")
    assert overall_status(latest_per_service()) == "operational"
    assert _component("plugins")["status"] == "operational"


# ── what yourbot.gg tells customers about this page ───────────────────────
#
# The main site describes this page in its own copy. Each claim is restated
# here against the real output, so a change that would make one false fails
# a test instead of reaching a customer.
#
#   /discord-bots/hosting   "needs no login and lists each part of the
#                            platform on its own, from the website and
#                            dashboard through the gateway, bot workers and
#                            plugin runner to the database and cache. Each
#                            part has a daily uptime bar for the last 90
#                            days. The page refreshes itself every 15 seconds
#                            and logs incidents as they are detected, with
#                            the last 7 days on the front page and 90 days in
#                            the history. You can follow it by RSS or send
#                            updates to your own Discord channel with a
#                            webhook."
#   /about                  "Live uptime, incident history and shard health
#                            on a public status page."
#   /security               "Uptime and incident history are public on the
#                            status page."

_STATIC = Path(status_service.__file__).resolve().parent / "static"


def _day(name: str, days_ago: int, failed: int, total: int = 1440) -> None:
    day = (NOW().date() - timedelta(days=days_ago)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_uptime(service_name, day, uptime_pct, total_checks, failed_checks) "
            "VALUES (?,?,?,?,?)", (name, day, round((total - failed) / total * 100.0, 3), total, failed))


def _shards(*rows: tuple[int, str, int | None, int]) -> None:
    """rows: (shard_id, status, latency_ms, servers)"""
    with db.connect() as conn:
        conn.execute("DELETE FROM shard_snapshot")
        for shard_id, status, latency, servers in rows:
            conn.execute(
                "INSERT INTO shard_snapshot(cluster_idx,shard_id,status,latency_ms,guild_count,fetched_at) "
                "VALUES (0,?,?,?,?,?)", (shard_id, status, latency, servers, _iso(NOW())))


def _stat(html: str, label: str) -> str:
    """The visible text of one figure in the "Right now" strip."""
    cells = re.findall(r'<div class="stat-cell">(.*?)</div>\s*(?=<div class="stat-cell">|</div>\s*</div>)', html, flags=re.S)
    for cell in cells:
        if label in cell:
            return " ".join(unescape(_visible_text(cell)).split())
    raise AssertionError(f"no figure labelled {label!r}")


def test_claim_no_login_is_needed():
    _all_up()
    with TestClient(app) as client:
        for path in ("/", "/history", "/live", "/feed.xml", "/api", "/api/incidents", "/badge.svg"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 200, path


def test_claim_the_page_refreshes_itself_every_15_seconds():
    script = (_STATIC / "status.js").read_text(encoding="utf-8")
    assert re.search(r"\bREFRESH_MS\s*=\s*15000\b", script)
    assert "fetch('/live'" in script and "setInterval" in script


def test_the_refresh_does_not_skip_what_the_visitor_is_looking_at():
    """Clicking "the checks behind this" leaves the keyboard focus on that
    line. The refresh used to skip any region that held the focus "until the
    next refresh", but the focus does not go away by itself: the statuses
    under an opened component froze for as long as the visitor looked at
    them, while the banner above moved on.

    What it does now is shown in a real browser by the live simulation
    (yb-status-verify/live_sim_130.py: the opened list goes to Down under the
    focus, the focus stays on the same line, and someone reading a 90-day bar
    with the arrow keys stays on the same day). This keeps the old skip from
    coming back unnoticed."""
    script = (_STATIC / "status.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in script.splitlines() if not line.strip().startswith("//"))
    assert "if (current.contains(document.activeElement)) return;" not in code
    assert "again.focus({ preventScroll: true })" in code
    # The one case that still waits: the focused element no longer exists, and
    # replacing the region would drop the focus to the top of the page.
    assert "if (focused && !again) return;" in code
    assert "restoreReading(reading)" in code


def test_claim_each_part_is_listed_and_has_a_daily_bar_for_90_days():
    _all_up()
    for name in ("Sandbox", "WebSocket Broker", "Image Service"):
        _probe(name, "operational")
    roll_up_after_probe()
    snap = build_snapshot()
    bars: dict[str, list[dict]] = {}
    for comp in snap["components"]:
        counted = [k for k in comp["checks"] if not k["advisory"]]
        for k in counted:
            # its own bar, or the component's where it is the only counted check
            bars[k["name"]] = k["cells"] if k["cells"] is not None else (comp["cells"] if len(counted) == 1 else None)
    # the parts the hosting page names, and the rest of the platform
    for name in ("Public Site", "Dashboard", "Gateway", "Bot Worker", "Bot", "Plugin Runner", "Database", "Cache",
                 "Orchestrator", "Analytics", "Sandbox", "WebSocket Broker", "Image Service"):
        assert name in bars, name
        assert bars[name] is not None and len(bars[name]) == 90, name
        assert bars[name][-1]["cls"] == "ok", name           # today, measured just now

    with TestClient(app) as client:
        html = client.get("/").text
    for label in ("Website", "Dashboard", "Gateway", "Bot Worker", "Plugin Runner", "Database", "Cache"):
        assert f'<span class="check-name">{label}</span>' in html, label
    # a bar per component, and one more for each check that shares a component
    own = sum(1 for comp in snap["components"] for k in comp["checks"] if k["cells"])
    assert own == 9
    assert html.count('class="daybar"') == len(snap["components"]) == 8
    assert html.count('class="daybar daybar-sm"') == own


def test_a_check_bar_shows_that_check_not_its_neighbour():
    """Two checks behind one component, one bad day each on different days:
    each bar marks its own day, and the component's bar marks both."""
    _all_up()
    for back in range(0, 5):
        _day("Bot Worker", back, failed=30 if back == 3 else 0)
        _day("Bot", back, failed=2 if back == 1 else 0)
    snap = build_snapshot()
    comp = next(c for c in snap["components"] if c["key"] == "commands")
    by_name = {k["name"]: k for k in comp["checks"]}

    def marks(cells: list[dict]) -> dict[int, str]:
        return {89 - i: c["cls"] for i, c in enumerate(cells) if c["cls"] in ("minor", "major")}

    assert marks(by_name["Bot Worker"]["cells"]) == {3: "major"}      # 30 of 1440 failed: over 1%
    assert marks(by_name["Bot"]["cells"]) == {1: "minor"}
    assert marks(comp["cells"]) == {3: "major", 1: "minor"}
    assert by_name["Bot Worker"]["timeline_summary"] == "Last 90 days: 5 measured, 1 with downtime"
    assert "about 30 min of downtime" in by_name["Bot Worker"]["cells"][86]["tip"]


def test_diagnostics_and_single_check_components_get_no_second_bar():
    _all_up()
    _probe("SSL Certificate", "operational", source="ssl")
    roll_up_after_probe()
    snap = build_snapshot()
    web = next(c for c in snap["components"] if c["key"] == "website")
    by_name = {k["name"]: k for k in web["checks"]}
    assert by_name["DNS"]["cells"] is None and by_name["SSL Certificate"]["cells"] is None
    assert by_name["Public Site"]["cells"] and by_name["Dashboard"]["cells"]
    gateway = next(c for c in snap["components"] if c["key"] == "bot")
    assert [k["cells"] for k in gateway["checks"]] == [None]            # the bar above is the Gateway's
    assert len(gateway["cells"]) == 90


def test_claim_incidents_last_7_days_on_the_front_page_and_90_in_the_history():
    _all_up()
    recent = _incident("Public Site", NOW() - timedelta(days=3), 12)
    older = _incident("Gateway", NOW() - timedelta(days=40), 25)
    ancient = _incident("Database", NOW() - timedelta(days=120), 25)
    with TestClient(app) as client:
        front, history = client.get("/").text, client.get("/history").text
    assert f'id="incident-{recent}"' in front and f'id="incident-{older}"' not in front
    assert f'id="incident-{recent}"' in history and f'id="incident-{older}"' in history
    assert f'id="incident-{ancient}"' not in history
    assert "Last 7 days" in front


def test_claim_follow_by_rss_or_your_own_discord_webhook():
    _all_up()
    with TestClient(app) as client:
        html = client.get("/").text
        feed = client.get("/feed.xml")
    assert 'href="/feed.xml"' in html
    assert 'action="/subscribe/webhook"' in html and 'name="url"' in html
    assert feed.status_code == 200 and "xml" in feed.headers["content-type"]
    minidom.parseString(feed.content)


def test_claim_shard_health_is_on_the_page():
    _all_up()
    _shards((0, "operational", 46, 861))
    with TestClient(app) as client:
        html = client.get("/").text
        assert _stat(html, "Shards online") == "1 of 1 Shards online the shared bot's connections to Discord"
        assert '1 <span class="stat-unit">of 1</span>' in html          # a real space: it must not read "1of 1"
        assert _stat(html, "Bot to Discord").startswith("46 ms")
        assert 'class="shard-list"' not in html                          # one shard: the figure says it all

        # the shard list could not be read: nothing is claimed, least of all "0 online"
        _shards((0, "unknown", None, 861))
        html = client.get("/").text
        assert _stat(html, "Shards online") == "– Shards online not measured right now"

        # several shards, one of them down
        _shards((0, "operational", 40, 500), (1, "down", None, 300), (2, "degraded", 310, 250), (3, "unknown", None, 90))
        html = client.get("/").text
        assert _stat(html, "Shards online") == "1 of 4 Shards online 1 degraded, 1 down, 1 not measured"
        rows = re.findall(r'<li class="shard">(.*?)</li>', html, flags=re.S)
        assert [" ".join(_visible_text(r).split()) for r in rows] == [
            "Shard 0 Online 40 ms 500 servers",
            "Shard 1 Down – 300 servers",
            "Shard 2 Degraded 310 ms 250 servers",
            "Shard 3 No data – 90 servers",
        ]
        totals = client.get("/api/shards").json()["totals"]
    assert totals == {"shards": 4, "guilds": 1140, "online": 1, "degraded": 1, "down": 1, "unknown": 1}


def test_the_page_and_its_refresh_are_sent_compressed():
    """Every open tab fetches /live every 15 seconds, over the monitor's own
    connection. The 90-day bars make it large and very repetitive."""
    _all_up()
    for name in ("Sandbox", "WebSocket Broker", "Image Service"):
        _probe(name, "operational")
    roll_up_after_probe()
    with TestClient(app) as client:
        plain = client.get("/live", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in plain.headers
        for path in ("/live", "/"):
            with client.stream("GET", path, headers={"Accept-Encoding": "gzip"}) as r:
                wire = b"".join(r.iter_raw())
                assert r.headers["content-encoding"] == "gzip", path
                assert {v.strip() for v in r.headers["vary"].split(",")} >= {"Accept", "Accept-Encoding"}
                assert r.headers["cache-control"] == "no-store, max-age=0"       # still never cached
            assert len(wire) < len(plain.content) / 8, (path, len(wire), len(plain.content))
        # and it unpacks to the same page (only the age of the last check moves between two requests)
        ageless = lambda page: re.sub(r' data-age="[^"]*"', "", page)  # noqa: E731
        assert ageless(gzip.decompress(wire).decode("utf-8")) == ageless(client.get("/").text)


# ── rate limiting ─────────────────────────────────────────────────────────

def test_each_visitor_has_their_own_rate_limit():
    """Behind the tunnel every request arrives from one address. Keyed on
    that, the whole internet shared sixty page loads a minute, so the page
    answered "Rate limit exceeded" exactly when an incident sent people to
    it. Visitors are told apart by the address Cloudflare supplies."""
    with TestClient(app) as client:
        codes = {client.get("/history", headers={"CF-Connecting-IP": f"203.0.113.{i}"}).status_code
                 for i in range(1, 80)}
        assert codes == {200}                               # 79 different visitors, none refused

        one = [client.get("/history", headers={"CF-Connecting-IP": "198.51.100.7"}).status_code
               for _ in range(61)]
        assert one[:60] == [200] * 60 and one[60] == 429    # one visitor still has a limit
        # and that visitor being limited does not touch anyone else
        assert client.get("/history", headers={"CF-Connecting-IP": "198.51.100.8"}).status_code == 200


def test_rate_limit_falls_back_to_the_socket_address(monkeypatch):
    monkeypatch.setenv("CLIENT_IP_HEADER", "")
    with TestClient(app) as client:
        codes = [client.get("/history", headers={"CF-Connecting-IP": f"203.0.113.{i}"}).status_code
                 for i in range(1, 62)]
    assert codes[59] == 200 and codes[60] == 429            # header ignored: one shared bucket again
