"""Re-apply today's measuring rules to the history already stored.

    python -m status_service.remeasure            # dry run: report only
    python -m status_service.remeasure --apply    # back up, then correct

Until the rework the prober did three things that put downtime on the page
which the platform never had:

1. GUESSED ROWS. When the one outside check of the website failed, every
   other service was written `down` for that minute without being looked at.
   Those rows are not measurements. They become `unknown` ("no data").

2. THE MONITOR'S OWN OUTAGES. The website check was one request with no
   retry, sent from one machine. When that machine could not even look the
   domain up, its own connection was what failed: on 2026-10-02 and
   2026-09-21 production served every other visitor normally while the page
   reported a three-minute outage. Those checks become `unknown`.

3. UNCONFIRMED FAILURES. A check that could not connect at all, once, with
   a good check a minute before and a minute after, proves nothing: the
   load balancer's log for 2026-10-04..06 shows all 11 such "failures" as
   requests that never arrived, while the site answered all 4,309 that did.
   They become `unknown`. (`--keep-isolated` leaves these alone.)

What is NEVER touched: a check the platform answered with an error, a check
that connected and then got no answer in time, and a failure that repeated
on the next check. Anything that reached YourBot and went wrong stays
counted as downtime.

Raw checks are only kept for 30 days. For older days the guessed rows can
still be removed exactly, by arithmetic: each failed website check produced
exactly one guessed `down` for each fanned-out service, so that count is
subtracted from the service's failures (and from its checks) for the day.
The website's own older days cannot be re-examined and are left as they are.

Nothing is deleted from probe_results: a corrected row keeps what it used to
say in `status_orig`, and a corrected day keeps its published figures in
`daily_uptime_orig`. `--apply` first writes a full copy of the database next
to it (status.db.bak-<time>), which is also the way back. Running it again
finds nothing left to do.
"""

from __future__ import annotations

import argparse
import bisect
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import db
from .aggregator import _parse_iso, _pct, _to_iso
from .config import get_settings

# What the old prober wrote on a guessed row (probes/http.py and
# scheduler.py before the rework).
GUESSED_ERROR = "public site unreachable"

# The services the old prober fanned a website failure out to. Database and
# Cache got theirs through derive_db_redis.
FANOUT_SERVICES = (
    "Dashboard", "Gateway", "Plugin Runner", "Orchestrator", "Bot", "Bot Worker",
    "Analytics", "Sandbox", "Image Service", "WebSocket Broker", "Database", "Cache",
)

SITE = "Public Site"
DNS = "DNS"
_SAME_CYCLE = 45.0  # seconds: two rows this close belong to the same probe cycle
_RESOLVER_HINTS = ("name resolution", "name or service not known", "getaddrinfo", "nodename nor servname",
                   "no address associated")

# The old prober's timeouts were connect=3s, read=5s, and it stored the
# elapsed time. A timeout past this mark therefore got as far as connecting
# and sending the request, i.e. it reached the platform.
_READ_TIMEOUT_MS = 4500

REASON_GUESSED = "not measured: website unreachable"
REASON_MONITOR = "monitor offline"
REASON_UNCONFIRMED = "unconfirmed: could not connect once, not retried"
REASON_DNS_SITE_OK = "lookup failed while the site answered"

# How one failed website check is read. The first three stand as downtime.
ANSWERED = "answered"        # the platform replied with an error status
NO_ANSWER = "no_answer"      # connected, then no reply in time
REPEATED = "repeated"        # could not connect, and the next check failed too
MONITOR = "monitor"          # the monitor could not resolve the domain
UNCONFIRMED = "unconfirmed"  # could not connect, once, good checks either side
KEPT = (ANSWERED, NO_ANSWER, REPEATED)


def _day(iso: str) -> str:
    return iso[:10]


def analyze(conn: sqlite3.Connection, keep_isolated: bool = False) -> dict:
    """Work out every change without writing anything."""
    bounds = conn.execute("SELECT MIN(checked_at) AS lo, MAX(checked_at) AS hi FROM probe_results").fetchone()
    raw_lo = bounds["lo"]
    changes: dict[int, tuple[str, str, str]] = {}  # id -> (service, day, reason)

    # ── 1. guessed rows ──────────────────────────────────────────────────
    for r in conn.execute(
        "SELECT id, service_name, checked_at FROM probe_results WHERE status='down' AND error=?",
        (GUESSED_ERROR,),
    ):
        changes[r["id"]] = (r["service_name"], _day(r["checked_at"]), REASON_GUESSED)

    # ── 2 + 3. the website check itself ──────────────────────────────────
    site_rows = conn.execute(
        "SELECT id, status, checked_at, error, http_status, response_ms FROM probe_results "
        "WHERE service_name=? ORDER BY id",
        (SITE,),
    ).fetchall()
    dns_rows = conn.execute(
        "SELECT id, status, checked_at FROM probe_results WHERE service_name=? ORDER BY id",
        (DNS,),
    ).fetchall()
    dns_times = [_parse_iso(r["checked_at"]).timestamp() for r in dns_rows]

    def dns_failed_near(ts: float) -> bool:
        i = bisect.bisect_left(dns_times, ts - _SAME_CYCLE)
        while i < len(dns_times) and dns_times[i] <= ts + _SAME_CYCLE:
            if dns_rows[i]["status"] == "down":
                return True
            i += 1
        return False

    site_verdict: dict[int, str] = {}   # index in site_rows -> one of the readings above
    n = len(site_rows)
    good = ("operational", "degraded")
    for k, row in enumerate(site_rows):
        if row["status"] != "down":
            continue
        err = (row["error"] or "").lower()
        if row["http_status"] is not None:
            site_verdict[k] = ANSWERED
        elif any(h in err for h in _RESOLVER_HINTS):
            site_verdict[k] = MONITOR
        elif err == "timeout" and int(row["response_ms"] or 0) >= _READ_TIMEOUT_MS:
            site_verdict[k] = NO_ANSWER
        elif dns_failed_near(_parse_iso(row["checked_at"]).timestamp()):
            site_verdict[k] = MONITOR
        else:
            flanked = (0 < k < n - 1
                       and site_rows[k - 1]["status"] in good and site_rows[k + 1]["status"] in good)
            site_verdict[k] = UNCONFIRMED if (flanked and not keep_isolated) else REPEATED

    for k, verdict in site_verdict.items():
        if verdict in KEPT:
            continue
        row = site_rows[k]
        reason = REASON_MONITOR if verdict == MONITOR else REASON_UNCONFIRMED
        changes[row["id"]] = (SITE, _day(row["checked_at"]), reason)

    # ── DNS: a failed lookup only counts if the site was unreachable too ──
    site_times = [_parse_iso(r["checked_at"]).timestamp() for r in site_rows]

    def site_index_near(ts: float) -> int | None:
        i = bisect.bisect_left(site_times, ts - _SAME_CYCLE)
        best, best_d = None, _SAME_CYCLE + 1
        while i < len(site_times) and site_times[i] <= ts + _SAME_CYCLE:
            d = abs(site_times[i] - ts)
            if d < best_d:
                best, best_d = i, d
            i += 1
        return best

    for r in dns_rows:
        if r["status"] != "down":
            continue
        k = site_index_near(_parse_iso(r["checked_at"]).timestamp())
        if k is None:
            continue
        if site_rows[k]["status"] != "down":
            changes[r["id"]] = (DNS, _day(r["checked_at"]), REASON_DNS_SITE_OK)
        elif site_verdict.get(k) in (MONITOR, UNCONFIRMED):
            changes[r["id"]] = (DNS, _day(r["checked_at"]), REASON_MONITOR)

    # ── daily figures, before and after ──────────────────────────────────
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"up": 0, "down": 0})
    for r in conn.execute(
        """
        SELECT service_name, substr(checked_at, 1, 10) AS day,
               SUM(CASE WHEN status IN ('operational','degraded') THEN 1 ELSE 0 END) AS up,
               SUM(CASE WHEN status='down' THEN 1 ELSE 0 END) AS down
        FROM probe_results GROUP BY service_name, day
        """
    ):
        if r["service_name"].startswith("__"):
            continue
        counts[(r["service_name"], r["day"])] = {"up": int(r["up"] or 0), "down": int(r["down"] or 0)}
    for service, day, _reason in changes.values():
        counts[(service, day)]["down"] -= 1   # every corrected row was a `down`

    # Days whose raw checks are all still present. The oldest day in the
    # table was partly pruned, so it is handled with the older days below.
    raw_days: set[str] = set()
    first_full_day = None
    if raw_lo:
        lo = _parse_iso(raw_lo)
        first_full = lo.date() if (lo.hour == 0 and lo.minute < 3) else (lo + timedelta(days=1)).date()
        first_full_day = first_full.isoformat()
        raw_days = {day for (_svc, day) in counts if day >= first_full_day}

    existing = {(r["service_name"], r["day"]): dict(r) for r in conn.execute(
        "SELECT service_name, day, uptime_pct, total_checks, failed_checks FROM daily_uptime")}
    # Figures as first published, for rows a previous run already corrected.
    # The arithmetic below always starts from these, so running twice does
    # not subtract twice.
    original = {(r["service_name"], r["day"]): dict(r) for r in conn.execute(
        "SELECT service_name, day, uptime_pct, total_checks, failed_checks FROM daily_uptime_orig")}

    daily_new: dict[tuple[str, str], dict | None] = {}
    for (service, day), c in counts.items():
        if day not in raw_days:
            continue
        pct = _pct(c["up"], c["down"])
        daily_new[(service, day)] = None if pct is None else {
            "uptime_pct": pct, "total_checks": c["up"] + c["down"], "failed_checks": c["down"]}

    # ── older days: remove the guessed rows by arithmetic ────────────────
    for key in set(existing) | set(original):
        service, day = key
        if day in raw_days or (first_full_day and day >= first_full_day):
            continue
        if service not in FANOUT_SERVICES:
            continue
        row = original.get(key) or existing[key]
        site = original.get((SITE, day)) or existing.get((SITE, day))
        guessed = int(site["failed_checks"]) if site else 0
        if guessed <= 0 or int(row["failed_checks"]) <= 0:
            continue
        failed = max(0, int(row["failed_checks"]) - guessed)
        total = max(failed, int(row["total_checks"]) - guessed)
        pct = _pct(total - failed, failed)
        daily_new[(service, day)] = None if pct is None else {
            "uptime_pct": pct, "total_checks": total, "failed_checks": failed}

    daily_changes = {}
    for key, new in daily_new.items():
        old = existing.get(key)
        if old is None and new is None:
            continue
        if (old is not None and new is not None
                and int(old["total_checks"]) == new["total_checks"]
                and int(old["failed_checks"]) == new["failed_checks"]):
            continue
        daily_changes[key] = (old, new)

    # ── incidents no longer supported by any confirmed-down check ────────
    changed_ids = set(changes)
    incident_drop: list[dict] = []
    incident_keep_explained: list[dict] = []
    site_incidents = [dict(r) for r in conn.execute(
        "SELECT id, started_at, ended_at FROM incidents WHERE service_name=? AND resolved=1", (SITE,))]
    for inc in conn.execute(
        "SELECT id, service_name, started_at, ended_at, duration_min, cause FROM incidents WHERE resolved=1"
    ):
        inc = dict(inc)
        if not inc["ended_at"]:
            continue
        in_raw = bool(first_full_day and _day(inc["started_at"]) >= first_full_day)
        if in_raw:
            lo = _to_iso(_parse_iso(inc["started_at"]) - timedelta(seconds=_SAME_CYCLE))
            hi = _to_iso(_parse_iso(inc["ended_at"]) + timedelta(seconds=_SAME_CYCLE))
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM probe_results WHERE service_name=? AND status='down' AND checked_at BETWEEN ? AND ?",
                (inc["service_name"], lo, hi))]
            unsupported = bool(ids) and all(i in changed_ids for i in ids)
        else:
            # No raw rows left: a fanned-out service's incident that sits
            # inside a website incident is one of the guesses.
            unsupported = False
            if inc["service_name"] in FANOUT_SERVICES:
                slack = timedelta(seconds=90)
                start, end = _parse_iso(inc["started_at"]), _parse_iso(inc["ended_at"])
                for site_inc in site_incidents:
                    if not site_inc["ended_at"]:
                        continue
                    if (_parse_iso(site_inc["started_at"]) - slack <= start
                            and end <= _parse_iso(site_inc["ended_at"]) + slack):
                        unsupported = True
                        break
        if not unsupported:
            continue
        (incident_keep_explained if inc["cause"] else incident_drop).append(inc)

    reasons: dict[str, int] = defaultdict(int)
    for _svc, _d, reason in changes.values():
        reasons[reason] += 1
    site_summary = defaultdict(int)
    for verdict in site_verdict.values():
        site_summary[verdict] += 1

    return {
        "raw_from": raw_lo, "raw_to": bounds["hi"], "first_full_day": first_full_day,
        "changes": changes, "reasons": dict(reasons), "site_summary": dict(site_summary),
        "daily_changes": daily_changes, "existing": existing,
        "incident_drop": incident_drop, "incident_keep_explained": incident_keep_explained,
    }


def _window_average(rows: dict[tuple[str, str], dict], service: str, days: list[str]) -> float | None:
    up = down = 0
    for day in days:
        row = rows.get((service, day))
        if not row:
            continue
        down += int(row["failed_checks"])
        up += int(row["total_checks"]) - int(row["failed_checks"])
    return _pct(up, down)


def report(plan: dict, out=sys.stdout) -> None:
    w = out.write
    w("Stored raw checks: %s to %s\n" % (plan["raw_from"], plan["raw_to"]))
    w("Days re-examined check by check: from %s\n\n" % plan["first_full_day"])

    w("Rows that stop counting as downtime: %d\n" % len(plan["changes"]))
    for reason, count in sorted(plan["reasons"].items(), key=lambda kv: -kv[1]):
        w("  %6d  %s\n" % (count, reason))
    s = plan["site_summary"]
    w("\nWebsite checks that had failed: %d\n" % sum(s.values()))
    w("  kept as downtime\n")
    w("  %6d  the platform answered with an error\n" % s.get(ANSWERED, 0))
    w("  %6d  connected, then no answer in time\n" % s.get(NO_ANSWER, 0))
    w("  %6d  could not connect, and the next check failed too\n" % s.get(REPEATED, 0))
    w("  no longer counted\n")
    w("  %6d  the monitor could not look the domain up (its own connection)\n" % s.get(MONITOR, 0))
    w("  %6d  could not connect once, good checks a minute either side\n" % s.get(UNCONFIRMED, 0))

    after = dict(plan["existing"])
    for key, (_old, new) in plan["daily_changes"].items():
        if new is None:
            after.pop(key, None)
        else:
            after[key] = new
    today = datetime.now(timezone.utc).date()
    last30 = [(today - timedelta(days=d)).isoformat() for d in range(30)]
    services = sorted({svc for (svc, _d) in plan["existing"]} | {svc for (svc, _d) in after})
    w("\n30-day uptime, before -> after\n")
    for svc in services:
        b, a = _window_average(plan["existing"], svc, last30), _window_average(after, svc, last30)
        if b is None and a is None:
            continue
        mark = "" if b == a else "   *"
        w("  %-20s %8s -> %8s%s\n" % (svc, "-" if b is None else "%.3f%%" % b, "-" if a is None else "%.3f%%" % a, mark))

    by_day: dict[str, int] = defaultdict(int)
    for (_svc, day) in plan["daily_changes"]:
        by_day[day] += 1
    w("\nDays with corrected figures: %d (%d service-days)\n" % (len(by_day), len(plan["daily_changes"])))
    site_days = sorted(
        (day, old, new) for (svc, day), (old, new) in plan["daily_changes"].items()
        if svc == SITE and (old is None or new is None or old["failed_checks"] != new["failed_checks"]))
    if site_days:
        w("Website, day by day:\n")
        for day, old, new in site_days:
            o = "%.2f%% (%d failed)" % (old["uptime_pct"], old["failed_checks"]) if old else "no data"
            n = "%.2f%% (%d failed)" % (new["uptime_pct"], new["failed_checks"]) if new else "no data"
            w("  %s  %-22s -> %s\n" % (day, o, n))

    w("\nIncident rows with no confirmed-down check left: %d (removed on --apply)\n" % len(plan["incident_drop"]))
    by_start: dict[str, list[str]] = defaultdict(list)
    for inc in plan["incident_drop"]:
        by_start[inc["started_at"][:16]].append(inc["service_name"])
    for start in sorted(by_start, reverse=True)[:40]:
        names = by_start[start]
        w("  %s  %s\n" % (start, ", ".join(names) if len(names) <= 4 else "%d services" % len(names)))
    if plan["incident_keep_explained"]:
        w("\nKept because someone wrote an explanation on them: %d\n" % len(plan["incident_keep_explained"]))
        for inc in plan["incident_keep_explained"]:
            w("  #%d %s %s\n" % (inc["id"], inc["service_name"], inc["started_at"]))


def apply(conn: sqlite3.Connection, plan: dict) -> None:
    """Write the plan in one short transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "UPDATE probe_results SET status_orig=COALESCE(status_orig, status), status='unknown', error=? "
            "WHERE id=? AND status='down'",
            [(reason, row_id) for row_id, (_svc, _day_, reason) in plan["changes"].items()],
        )
        for (service, day), (_old, new) in plan["daily_changes"].items():
            conn.execute(
                "INSERT OR IGNORE INTO daily_uptime_orig(service_name, day, uptime_pct, total_checks, failed_checks) "
                "SELECT service_name, day, uptime_pct, total_checks, failed_checks FROM daily_uptime "
                "WHERE service_name=? AND day=?",
                (service, day),
            )
            if new is None:
                conn.execute("DELETE FROM daily_uptime WHERE service_name=? AND day=?", (service, day))
            else:
                conn.execute(
                    "INSERT INTO daily_uptime(service_name, day, uptime_pct, total_checks, failed_checks) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(service_name, day) DO UPDATE SET "
                    "uptime_pct=excluded.uptime_pct, total_checks=excluded.total_checks, "
                    "failed_checks=excluded.failed_checks",
                    (service, day, new["uptime_pct"], new["total_checks"], new["failed_checks"]),
                )
        conn.executemany("DELETE FROM incidents WHERE id=?", [(inc["id"],) for inc in plan["incident_drop"]])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def backup(db_path: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = f"{db_path}.bak-{stamp}"
    src = sqlite3.connect(db_path, timeout=30.0)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m status_service.remeasure",
        description="Re-apply the current measuring rules to stored history. Dry run unless --apply is given.",
    )
    parser.add_argument("--apply", action="store_true", help="back up the database, then write the corrections")
    parser.add_argument("--keep-isolated", action="store_true",
                        help="leave single failed website checks counted as downtime")
    parser.add_argument("--no-backup", action="store_true", help="with --apply: skip the backup copy")
    args = parser.parse_args(argv)

    db.init_db()  # adds probe_results.status_orig on a database that predates it
    db_path = get_settings().db_path
    with db.connect() as conn:
        plan = analyze(conn, keep_isolated=args.keep_isolated)
        report(plan)
        if not args.apply:
            print("\nDry run. Nothing was changed. Re-run with --apply to write this.")
            return 0
        if not plan["changes"] and not plan["daily_changes"] and not plan["incident_drop"]:
            print("\nNothing to correct.")
            return 0
        if not args.no_backup:
            print("\nBacking up to", backup(db_path))
        apply(conn, plan)
    print("Applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
