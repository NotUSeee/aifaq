"""The history-correction tool only re-reads the OLD prober's checks.

The current prober retries a failed check and tests its own connection
before it records one. A failure it stored is confirmed, however short, so
reading it again by the old prober's habits ("one lone connect failure
proves nothing") would erase real downtime. The database remembers when the
current rules took over, and nothing from that moment on is touched.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from status_service import db, remeasure
from tests.test_remeasure import DAY0, FANOUT, History, _apply, _iso, _site_statuses


def _rules_took_over_at(minute: int) -> str:
    """Pretend the current scheduler's first cycle was at this minute."""
    since = _iso(DAY0 + timedelta(minutes=minute))
    with db.connect() as conn:
        conn.execute("DELETE FROM meta_kv WHERE key=?", (db.RULES_SINCE_KEY,))
        conn.execute(
            "INSERT INTO probe_results(service_name,status,response_ms,http_status,error,source,checked_at) "
            "VALUES ('__monitor__','operational',NULL,NULL,NULL,'monitor',?)", (since,))
        assert db.ensure_rules_since(conn) == since
    return since


def _new_failure(minute: int, error: str = "All connection attempts failed", dns: str = "operational") -> None:
    """A website failure as the CURRENT prober stores it: confirmed, with the
    services behind it written as no data, not down."""
    ts = _iso(DAY0 + timedelta(minutes=minute))
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO probe_results(service_name,status,response_ms,http_status,error,source,checked_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [("Public Site", "down", 3010, None, error, "external", ts),
             ("DNS", dns, 12, None, None if dns == "operational" else "resolve timeout", "dns", ts),
             ("__monitor__", "operational", None, None, None, "monitor", ts)]
            + [(name, "unknown", None, None, "not measured: website unreachable", "proxy", ts) for name in FANOUT])


def test_a_lone_failure_recorded_by_the_current_prober_is_never_rewritten():
    """The same shape of failure on both sides of the handover: the old one
    is unconfirmed, the new one was confirmed when it was made."""
    History().cycle(0).cycle(1, site="connect_timeout").cycle(2).write()
    since = _rules_took_over_at(8)
    History().cycle(9).write()
    _new_failure(10)
    History().cycle(11).write()
    plan = _apply()
    assert plan["rules_since"] == since
    assert plan["site_summary"] == {remeasure.UNCONFIRMED: 1}          # only the old one was even looked at
    assert _site_statuses() == ["operational", "unknown", "operational", "operational", "down", "operational"]
    with db.connect() as conn:
        kept = conn.execute("SELECT status, status_orig FROM probe_results WHERE service_name='Public Site' "
                            "AND checked_at=?", (_iso(DAY0 + timedelta(minutes=10)),)).fetchone()
    assert kept["status"] == "down" and kept["status_orig"] is None


@pytest.mark.parametrize("error", ["[Errno -3] Temporary failure in name resolution", "timeout",
                                   "All connection attempts failed"])
def test_no_kind_of_failure_after_the_handover_is_touched(error):
    """Not a failed lookup either: the current prober only stores one when
    its own connection was working, so the domain really did not resolve."""
    History().cycle(0).write()
    _rules_took_over_at(8)
    History().cycle(9).write()
    _new_failure(10, error=error, dns="down")
    History().cycle(11).write()
    plan = _apply()
    assert plan["changes"] == {} and plan["site_summary"] == {}
    with db.connect() as conn:
        rows = {r["service_name"]: r["status"] for r in conn.execute(
            "SELECT service_name, status FROM probe_results WHERE checked_at=?", (_iso(DAY0 + timedelta(minutes=10)),))}
    assert rows["Public Site"] == "down" and rows["DNS"] == "down"


def test_an_incident_that_began_after_the_handover_is_never_dropped():
    History().cycle(0).cycle(1, site="connect_timeout").cycle(2).write()
    _rules_took_over_at(8)
    with db.connect() as conn:
        for name, minute in (("Dashboard", 1), ("Gateway", 20)):
            conn.execute(
                "INSERT INTO incidents(service_name, started_at, ended_at, duration_min, resolved) VALUES (?,?,?,?,1)",
                (name, _iso(DAY0 + timedelta(minutes=minute)), _iso(DAY0 + timedelta(minutes=minute + 3)), 3))
        # the Gateway really was down for those minutes, as the platform reported it
        conn.executemany(
            "INSERT INTO probe_results(service_name,status,response_ms,http_status,error,source,checked_at) "
            "VALUES ('Gateway','down',NULL,NULL,NULL,'proxy',?)",
            [(_iso(DAY0 + timedelta(minutes=m)),) for m in (20, 21, 22)])
    plan = _apply()
    assert [i["service_name"] for i in plan["incident_drop"]] == ["Dashboard"]      # the guessed one, from before
    with db.connect() as conn:
        left = [r["service_name"] for r in conn.execute("SELECT service_name FROM incidents ORDER BY id")]
    assert left == ["Gateway"]


def test_older_days_are_not_adjusted_once_the_current_rules_were_in_force():
    """The arithmetic for days whose raw checks are gone removes one guessed
    failure per failed website check. From the handover on there are no
    guessed failures, so those days must be left exactly as published."""
    History().cycle(0).write()
    before_day = (DAY0 - timedelta(days=40)).date().isoformat()     # old rules, raw checks long gone
    after_day = (DAY0 - timedelta(days=20)).date().isoformat()      # current rules, raw checks gone too
    with db.connect() as conn:
        conn.execute("DELETE FROM meta_kv WHERE key=?", (db.RULES_SINCE_KEY,))
        conn.execute("INSERT INTO meta_kv(key, value) VALUES (?,?)",
                     (db.RULES_SINCE_KEY, _iso(DAY0 - timedelta(days=30))))
        for day in (before_day, after_day):
            conn.execute("INSERT INTO daily_uptime(service_name, day, uptime_pct, total_checks, failed_checks) "
                         "VALUES ('Public Site',?,99.306,1440,10)", (day,))
            conn.execute("INSERT INTO daily_uptime(service_name, day, uptime_pct, total_checks, failed_checks) "
                         "VALUES ('Gateway',?,99.167,1440,12)", (day,))
    plan = _apply()
    changed_days = {day for (svc, day) in plan["daily_changes"] if svc == "Gateway"}
    assert changed_days & {before_day, after_day} == {before_day}
    with db.connect() as conn:
        rows = {r["day"]: r["failed_checks"] for r in conn.execute(
            "SELECT day, failed_checks FROM daily_uptime WHERE service_name='Gateway' AND day IN (?,?)",
            (before_day, after_day))}
    assert rows == {before_day: 2, after_day: 12}        # 10 guessed failures removed before, none after


def test_the_handover_moment_is_remembered_once_and_never_moves():
    with db.connect() as conn:
        first = db.ensure_rules_since(conn)              # set by init_db when this test's database was made
        assert first and db.rules_since(conn) == first
    History().cycle(0).write()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO probe_results(service_name,status,source,checked_at) "
            "VALUES ('__monitor__','operational','monitor',?)", (_iso(DAY0),))
        assert db.ensure_rules_since(conn) == first      # an earlier heartbeat turning up later changes nothing
    db.init_db()
    with db.connect() as conn:
        assert db.rules_since(conn) == first


def test_a_database_first_run_before_the_moment_was_stored_uses_its_first_heartbeat():
    """The live server ran the current scheduler for a while before this
    was added: the first heartbeat row it wrote marks the handover."""
    History().cycle(0).cycle(1, site="connect_timeout").cycle(2).write()
    since = _rules_took_over_at(8)
    with db.connect() as conn:
        conn.execute("DELETE FROM meta_kv WHERE key=?", (db.RULES_SINCE_KEY,))
        assert db.rules_since(conn) == since             # derived, nothing stored yet
    db.init_db()                                         # the next start stores it
    with db.connect() as conn:
        stored = conn.execute("SELECT value FROM meta_kv WHERE key=?", (db.RULES_SINCE_KEY,)).fetchone()["value"]
    assert stored == since


def test_a_failed_lookup_after_the_handover_is_left_alone_even_if_the_site_answered():
    """The current prober does not store such a row as down in the first
    place. If one is ever there, it is still not this tool's to rewrite."""
    History().cycle(0).write()
    _rules_took_over_at(8)
    History().cycle(9).write()
    ts = _iso(DAY0 + timedelta(minutes=10))
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO probe_results(service_name,status,response_ms,http_status,error,source,checked_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [("Public Site", "operational", 160, 200, None, "external", ts),
             ("DNS", "down", 3000, None, "resolve timeout", "dns", ts)])
    History().cycle(11).write()
    plan = _apply()
    assert plan["changes"] == {}
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM probe_results WHERE service_name='DNS' AND checked_at=?",
                            (ts,)).fetchone()["status"] == "down"


def test_old_incidents_inside_a_website_incident_are_only_dropped_from_before_the_handover():
    """For days whose raw checks are gone, a fanned-out service's incident
    that sits inside a website incident was one of the guesses. Under the
    current rules it is not a guess: the service really was confirmed down."""
    History().cycle(0).write()
    with db.connect() as conn:
        conn.execute("DELETE FROM meta_kv WHERE key=?", (db.RULES_SINCE_KEY,))
        conn.execute("INSERT INTO meta_kv(key, value) VALUES (?,?)",
                     (db.RULES_SINCE_KEY, _iso(DAY0 - timedelta(days=30))))
        for days_back in (40, 20):
            start = DAY0 - timedelta(days=days_back)
            for name in ("Public Site", "Gateway"):
                conn.execute(
                    "INSERT INTO incidents(service_name, started_at, ended_at, duration_min, resolved) "
                    "VALUES (?,?,?,?,1)", (name, _iso(start), _iso(start + timedelta(minutes=6)), 6))
    plan = _apply()
    dropped = [(i["service_name"], i["started_at"][:10]) for i in plan["incident_drop"]]
    assert dropped == [("Gateway", (DAY0 - timedelta(days=40)).date().isoformat())]
    with db.connect() as conn:
        left = conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()["n"]
    assert left == 3
