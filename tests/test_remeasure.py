"""The history-correction tool: what it changes, and above all what it
must never change."""

from __future__ import annotations

import glob
import os
from datetime import datetime, timedelta, timezone

import pytest

from status_service import db, remeasure
from status_service.config import get_settings

FANOUT = ("Dashboard", "Gateway", "Bot", "Bot Worker")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Raw rows start at midnight three days back, so that day is complete.
DAY0 = (datetime.now(timezone.utc) - timedelta(days=3)).replace(hour=0, minute=0, second=30, microsecond=0)


class History:
    """Writes probe rows minute by minute in the shape the OLD prober used."""

    def __init__(self):
        self.rows = []

    def cycle(self, minute: int, site: str = "ok", dns: str = "ok", own_down: tuple[str, ...] = ()):
        ts = _iso(DAY0 + timedelta(minutes=minute))
        if site == "ok":
            self.rows.append(("Public Site", "operational", 170, 200, None, "external", ts))
            self.rows.append(("Database", "operational", 170, None, None, "external", ts))
            for name in FANOUT:
                st = "down" if name in own_down else "operational"
                self.rows.append((name, st, 9, None, None, "proxy", ts))
        else:
            http_status, ms, err = {
                "http_503": (503, 3037, None),
                "read_timeout": (None, 5210, "timeout"),
                "connect_timeout": (None, 3004, "timeout"),
                "resolver": (None, 40, "[Errno -3] Temporary failure in name resolution"),
            }[site]
            self.rows.append(("Public Site", "down", ms, http_status, err, "external", ts))
            self.rows.append(("Database", "down", None, None, remeasure.GUESSED_ERROR, "external", ts))
            for name in FANOUT:
                self.rows.append((name, "down", None, None, remeasure.GUESSED_ERROR, "proxy", ts))
        if dns == "ok":
            self.rows.append(("DNS", "operational", 12, None, None, "dns", ts))
        else:
            self.rows.append(("DNS", "down", 3000, None, "resolve timeout", "dns", ts))
        return self

    def write(self):
        with db.connect() as conn:
            conn.executemany(
                "INSERT INTO probe_results(service_name,status,response_ms,http_status,error,source,checked_at) "
                "VALUES (?,?,?,?,?,?,?)", self.rows)
        return self


def _plan(**kw) -> dict:
    with db.connect() as conn:
        return remeasure.analyze(conn, **kw)


def _apply() -> dict:
    with db.connect() as conn:
        plan = remeasure.analyze(conn)
        remeasure.apply(conn, plan)
    return plan


def _site_statuses() -> list[str]:
    with db.connect() as conn:
        return [r["status"] for r in conn.execute(
            "SELECT status FROM probe_results WHERE service_name='Public Site' ORDER BY id")]


def test_guessed_rows_become_no_data_and_keep_their_original():
    History().cycle(0).cycle(1, site="read_timeout").cycle(2).write()
    plan = _apply()
    assert plan["reasons"][remeasure.REASON_GUESSED] == len(FANOUT) + 1     # the fan-out plus Database
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, status_orig, error FROM probe_results WHERE service_name='Bot' ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["operational", "unknown", "operational"]
    assert rows[1]["status_orig"] == "down"
    assert rows[1]["error"] == remeasure.REASON_GUESSED


@pytest.mark.parametrize("kind", ["http_503", "read_timeout"])
def test_a_failure_that_reached_the_platform_is_never_touched(kind):
    """Even alone, with good checks either side: the platform answered with
    an error, or took the connection and never replied. That is downtime."""
    History().cycle(0).cycle(1, site=kind).cycle(2).write()
    plan = _apply()
    assert plan["site_summary"] == {remeasure.ANSWERED if kind == "http_503" else remeasure.NO_ANSWER: 1}
    assert _site_statuses() == ["operational", "down", "operational"]


def test_resolver_failure_is_the_monitors_own_connection():
    History().cycle(0).cycle(1, site="resolver", dns="down").cycle(2, site="resolver", dns="down").cycle(3).write()
    plan = _apply()
    assert plan["site_summary"] == {remeasure.MONITOR: 2}
    assert _site_statuses() == ["operational", "unknown", "unknown", "operational"]
    with db.connect() as conn:
        dns = [r["status"] for r in conn.execute("SELECT status FROM probe_results WHERE service_name='DNS' ORDER BY id")]
    assert dns == ["operational", "unknown", "unknown", "operational"]


def test_failed_lookup_in_the_same_minute_marks_a_connect_failure_as_the_monitors():
    History().cycle(0).cycle(1, site="connect_timeout", dns="down").cycle(2).write()
    assert _plan()["site_summary"] == {remeasure.MONITOR: 1}


def test_lone_connect_failure_between_good_checks_is_unconfirmed():
    History().cycle(0).cycle(1, site="connect_timeout").cycle(2).write()
    plan = _apply()
    assert plan["site_summary"] == {remeasure.UNCONFIRMED: 1}
    assert _site_statuses() == ["operational", "unknown", "operational"]


def test_connect_failure_that_repeats_stands_as_downtime():
    History().cycle(0).cycle(1, site="connect_timeout").cycle(2, site="connect_timeout").cycle(3).write()
    plan = _apply()
    assert plan["site_summary"] == {remeasure.REPEATED: 2}
    assert _site_statuses() == ["operational", "down", "down", "operational"]


def test_keep_isolated_leaves_lone_failures_alone():
    History().cycle(0).cycle(1, site="connect_timeout").cycle(2).write()
    assert _plan(keep_isolated=True)["site_summary"] == {remeasure.REPEATED: 1}


def test_failed_lookup_is_dropped_when_the_site_answered_that_minute():
    History().cycle(0).cycle(1, dns="down").cycle(2).write()
    plan = _apply()
    assert plan["reasons"] == {remeasure.REASON_DNS_SITE_OK: 1}


def test_a_services_own_failures_survive():
    """Reported by the platform itself while the website was fine: real."""
    History().cycle(0).cycle(1, own_down=("Gateway",)).cycle(2, own_down=("Gateway",)).cycle(3).write()
    plan = _apply()
    assert plan["changes"] == {}
    with db.connect() as conn:
        gw = [r["status"] for r in conn.execute("SELECT status FROM probe_results WHERE service_name='Gateway' ORDER BY id")]
    assert gw == ["operational", "down", "down", "operational"]


def test_daily_figures_are_rebuilt_from_the_corrected_rows():
    day = DAY0.date().isoformat()
    h = History()
    for m in range(10):
        if m == 4:
            h.cycle(m, site="connect_timeout")          # lone blip: unconfirmed
        elif m in (6, 7):
            h.cycle(m, site="read_timeout")             # real
        else:
            h.cycle(m)
    h.write()
    with db.connect() as conn:                          # as the old prober published it
        conn.execute("INSERT INTO daily_uptime VALUES ('Public Site', ?, 70.0, 10, 3)", (day,))
        conn.execute("INSERT INTO daily_uptime VALUES ('Bot', ?, 70.0, 10, 3)", (day,))
    _apply()
    with db.connect() as conn:
        rows = {r["service_name"]: dict(r) for r in conn.execute(
            "SELECT service_name, uptime_pct, total_checks, failed_checks FROM daily_uptime WHERE day=?", (day,))}
        orig = {r["service_name"]: dict(r) for r in conn.execute(
            "SELECT service_name, uptime_pct, failed_checks FROM daily_uptime_orig WHERE day=?", (day,))}
    assert (rows["Public Site"]["total_checks"], rows["Public Site"]["failed_checks"]) == (9, 2)
    assert rows["Public Site"]["uptime_pct"] == round(7 / 9 * 100, 3)
    assert (rows["Bot"]["total_checks"], rows["Bot"]["failed_checks"]) == (7, 0)     # never actually down
    assert rows["Bot"]["uptime_pct"] == 100.0
    assert orig["Bot"]["failed_checks"] == 3 and orig["Public Site"]["uptime_pct"] == 70.0


def test_older_days_lose_exactly_the_guessed_rows_and_only_once():
    """No raw rows left for these days. Each failed website check produced
    one guessed `down` per fanned-out service, so that count comes off."""
    History().cycle(0).cycle(1).write()                 # defines where raw data starts
    old_day = (DAY0 - timedelta(days=20)).date().isoformat()
    with db.connect() as conn:
        conn.executemany("INSERT INTO daily_uptime VALUES (?, ?, ?, ?, ?)", [
            ("Public Site", old_day, 97.153, 1440, 41),
            ("Bot", old_day, 97.153, 1440, 41),          # a pure copy of the website's failures
            ("Gateway", old_day, 91.528, 1440, 122),     # 41 guessed + 81 of its own
            ("Database", old_day, 98.555, 2838, 41),
            ("FAQ Matcher", old_day, 99.861, 1440, 2),   # was never fanned out
        ])
    _apply()

    def figures():
        with db.connect() as conn:
            return {r["service_name"]: (r["total_checks"], r["failed_checks"], r["uptime_pct"]) for r in conn.execute(
                "SELECT service_name, total_checks, failed_checks, uptime_pct FROM daily_uptime WHERE day=?", (old_day,))}

    first = figures()
    assert first["Bot"] == (1399, 0, 100.0)
    assert first["Gateway"][:2] == (1399, 81)
    assert first["Database"][:2] == (2797, 0)
    assert first["Public Site"] == (1440, 41, 97.153)    # cannot be re-examined: left alone
    assert first["FAQ Matcher"] == (1440, 2, 99.861)

    again = _plan()
    assert again["daily_changes"] == {} and again["changes"] == {}
    _apply()
    assert figures() == first                            # a second run subtracts nothing more


def test_incidents_made_only_of_guesses_are_removed_and_real_ones_kept():
    h = History()
    for m in range(12):
        if m in (3, 4, 5):
            h.cycle(m, site="resolver", dns="down")      # the monitor's own outage
        elif m in (8, 9):
            h.cycle(m, site="read_timeout")              # a real website outage
        else:
            h.cycle(m)
    h.write()

    def inc(name, first, last, cause=None):
        with db.connect() as conn:
            return int(conn.execute(
                "INSERT INTO incidents(service_name, started_at, ended_at, duration_min, resolved, cause) "
                "VALUES (?,?,?,?,1,?)",
                (name, _iso(DAY0 + timedelta(minutes=first)), _iso(DAY0 + timedelta(minutes=last + 1)),
                 last - first + 1, cause)).lastrowid)

    ghost_site = inc("Public Site", 3, 5)
    ghost_bot = inc("Bot", 3, 5)
    real_site = inc("Public Site", 8, 9)
    copied_bot = inc("Bot", 8, 9)                        # the bot was never down: a guess
    explained = inc("Gateway", 3, 5, cause="We wrote about this one.")

    plan = _apply()
    assert {i["id"] for i in plan["incident_drop"]} == {ghost_site, ghost_bot, copied_bot}
    assert [i["id"] for i in plan["incident_keep_explained"]] == [explained]
    with db.connect() as conn:
        left = {r["id"] for r in conn.execute("SELECT id FROM incidents")}
    assert left == {real_site, explained}


def test_dry_run_changes_nothing_and_apply_backs_up_first(capsys):
    History().cycle(0).cycle(1, site="connect_timeout").cycle(2).write()
    db_path = get_settings().db_path

    assert remeasure.main([]) == 0
    assert "Dry run. Nothing was changed." in capsys.readouterr().out
    assert _site_statuses() == ["operational", "down", "operational"]
    assert glob.glob(db_path + ".bak-*") == []

    assert remeasure.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "Backing up to" in out and "Applied." in out
    backups = glob.glob(db_path + ".bak-*")
    assert len(backups) == 1
    assert _site_statuses() == ["operational", "unknown", "operational"]

    # the backup still holds the data as it was
    import sqlite3
    conn = sqlite3.connect(backups[0])
    try:
        assert [r[0] for r in conn.execute(
            "SELECT status FROM probe_results WHERE service_name='Public Site' ORDER BY id")] == [
                "operational", "down", "operational"]
    finally:
        conn.close()

    assert remeasure.main(["--apply"]) == 0
    assert "Nothing to correct." in capsys.readouterr().out
    for path in backups:
        os.unlink(path)
