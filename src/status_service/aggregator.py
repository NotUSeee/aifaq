from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db
from .components import (
    ADVISORY_SERVICES,
    CRITICAL_SERVICES,
    GROUPS,
    HOW_LABEL,
    OPTIONAL_SERVICES,
    SERVICE_ORDER,
    UPTIME_EXCLUDED_SERVICES,
    Group,
    group_of,
    member_names,
    service_info,
    service_label,
)
from .config import get_settings

__all__ = [
    "OPTIONAL_SERVICES", "SERVICE_ORDER", "SERVICE_GROUPS", "SLA_EXCLUDED_SERVICES",
    "CurrentService", "latest_per_service", "overall_status", "build_components",
    "uptime_windows", "headline_uptime", "daily_uptime_series", "incident_events",
    "incidents_recent", "response_time_series", "shard_summary", "sla_summary",
    "monitor_state", "roll_up_after_probe", "seen_proxy_services", "recent_proxy_services",
    "group_currents",
]

# External dependencies and diagnostics excluded from OUR uptime — Discord's
# health isn't our availability, and the certificate and domain-lookup checks
# describe the plumbing of the website check rather than a separate service.
SLA_EXCLUDED_SERVICES = UPTIME_EXCLUDED_SERVICES

# A check older than this no longer describes "now". Applies to every source:
# if the scheduler stops, the page must fall back to "no data", not keep
# showing whatever it last saw.
STALE_AFTER_SECONDS = 300
STALE_AFTER_BY_SERVICE = {"SSL Certificate": 3 * 3600}  # probed hourly
_RECENT_WINDOW = timedelta(hours=4)

UPTIME_WINDOWS: tuple[tuple[str, int], ...] = (("24h", 1), ("7d", 7), ("30d", 30), ("90d", 90))
WINDOW_LABEL = {"24h": "24 hours", "7d": "7 days", "30d": "30 days", "90d": "90 days"}

_VERDICTS = ("operational", "degraded", "down")

# Back-compat for callers that still want (title, [service names]) pairs.
SERVICE_GROUPS: list[tuple[str, list[str]]] = [(g.name, list(g.services)) for g in GROUPS]


@dataclass
class CurrentService:
    name: str
    status: str
    response_ms: int | None
    checked_at: str
    error: str | None
    source: str = ""


# ── Current state ─────────────────────────────────────────────────────────

def _recent_latest_rows(conn) -> list:
    """Latest row per service among recent checks. Bounded by time so it
    rides the checked_at index instead of scanning the whole table."""
    cutoff = _to_iso(datetime.now(timezone.utc) - _RECENT_WINDOW)
    return conn.execute(
        """
        SELECT service_name, status, response_ms, checked_at, error, source
        FROM probe_results
        WHERE id IN (
          SELECT MAX(id) FROM probe_results WHERE checked_at >= ? GROUP BY service_name
        )
        """,
        (cutoff,),
    ).fetchall()


def latest_per_service() -> list[CurrentService]:
    """The latest check per service, ordered by SERVICE_ORDER. A check that
    is too old to describe the present is reported as 'unknown'."""
    with db.connect() as conn:
        rows = _recent_latest_rows(conn)
        ever = {r["service_name"] for r in conn.execute(
            "SELECT DISTINCT service_name FROM daily_uptime").fetchall()}

    by_name = {row["service_name"]: row for row in rows}
    now = datetime.now(timezone.utc)
    out: list[CurrentService] = []

    def _current(name: str, row) -> CurrentService:
        status, error = row["status"], row["error"]
        limit = STALE_AFTER_BY_SERVICE.get(name, STALE_AFTER_SECONDS)
        try:
            if (now - _parse_iso(row["checked_at"])).total_seconds() > limit:
                status, error = "unknown", "no recent check"
        except Exception:
            status, error = "unknown", "no recent check"
        return CurrentService(name=name, status=status, response_ms=row["response_ms"],
                              checked_at=row["checked_at"], error=error, source=row["source"] or "")

    for name in SERVICE_ORDER:
        row = by_name.get(name)
        if row is not None:
            out.append(_current(name, row))
        elif name in ever:
            out.append(CurrentService(name=name, status="unknown", response_ms=None,
                                      checked_at="", error="no recent check"))
        elif name not in OPTIONAL_SERVICES:
            out.append(CurrentService(name=name, status="unknown", response_ms=None,
                                      checked_at="", error="no data yet"))
        # else: optional and never reported here — hidden rather than a
        # permanent gray row.

    listed = set(SERVICE_ORDER)
    for name in sorted(by_name):
        if name in listed or name.startswith("__"):
            continue
        out.append(_current(name, by_name[name]))  # reported by the platform, not in our catalog yet
    return out


def seen_proxy_services() -> set[str]:
    """Service names the platform reported via /status/api in the last two
    days. Used by the scheduler to decide which services go to "no data"
    together when the platform cannot be read."""
    cutoff = _to_iso(datetime.now(timezone.utc) - timedelta(hours=48))
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT service_name FROM probe_results WHERE checked_at >= ? AND source='proxy'",
            (cutoff,),
        ).fetchall()
    return {r["service_name"] for r in rows}


def recent_proxy_services() -> set[str]:
    """Services whose latest recent row came from the platform's own report.

    This is the list that must go to "no data" together the moment that
    report cannot be read. It is read fresh from the same recent rows the
    page shows, so a service is covered from its very first report: an
    earlier cached "seen before" list left newly reported services showing
    their last "operational" for five minutes into an outage.
    """
    with db.connect() as conn:
        rows = _recent_latest_rows(conn)
    return {r["service_name"] for r in rows
            if r["source"] in ("proxy", "push") and not r["service_name"].startswith("__")}


def _is_third_party(name: str) -> bool:
    g = group_of(name)
    return bool(g and g.third_party)


def _effective(c: CurrentService) -> str:
    """Status as it bears on customers: a diagnostic's early warning
    (certificate expiring in two weeks) is not a degraded service."""
    if c.name in ADVISORY_SERVICES and c.status == "degraded":
        return "operational"
    return c.status


def overall_status(currents: list[CurrentService]) -> str:
    """Reduce per-service statuses to one verdict about YourBot itself.
    Third-party rows (Discord) never move it."""
    own = [c for c in currents if not _is_third_party(c.name)]
    # Diagnostics cannot vouch for the platform: an hour-old certificate
    # check saying "fine" must not turn "we have no data" into "all good".
    # They only weigh in when they have failed outright.
    primary = [c for c in own if c.name not in ADVISORY_SERVICES]
    advisory_down = any(c.status == "down" for c in own if c.name in ADVISORY_SERVICES)
    known = {c.status for c in primary} - {"unknown"}
    if not known and not advisory_down:
        return "unknown"
    if "down" in known or advisory_down:
        if any(c.status == "down" and c.name in CRITICAL_SERVICES for c in primary):
            return "outage"
        return "partial_outage"
    if "degraded" in known:
        return "degraded"
    if silent_core_components(currents):
        # Nothing we can see is failing, but part of the platform is not
        # being measured at all. "All systems operational" would be a claim
        # about systems nobody looked at.
        return "limited"
    return "operational"


def silent_core_components(currents: list[CurrentService]) -> list[str]:
    """Names of the everyday components whose every check has no data.

    Only components people use every day (core, ours) count, and only when
    ALL of their counted checks are silent: one quiet check next to a working
    one is shown on its own row and does not change the headline.
    """
    out: list[str] = []
    for group in GROUPS:
        if group.third_party or not group.core:
            continue
        members = [c for c in currents if group_of(c.name) is group and c.name not in ADVISORY_SERVICES]
        if members and all(c.status == "unknown" for c in members):
            out.append(group.name)
    return out


def _rollup(members: list[CurrentService]) -> str:
    """Status of a customer-facing component from the checks behind it:
    operational | degraded | partial | down | unknown."""
    primary = [c for c in members if c.name not in ADVISORY_SERVICES]
    advisory_down = [c for c in members if c.name in ADVISORY_SERVICES and c.status == "down"]
    known = [_effective(c) for c in primary if c.status != "unknown"]
    if not known and not advisory_down:
        return "unknown"
    downs = sum(1 for s in known if s == "down") + len(advisory_down)
    total = len(known) + len(advisory_down)
    if downs == total:
        return "down"
    if downs:
        return "partial"
    if "degraded" in known:
        return "degraded"
    return "operational"


def group_currents(currents: list[CurrentService]) -> list[dict]:
    """Bucket the flat current-service list into the customer-facing groups,
    appending anything uncatalogued as a final 'Other' group so nothing the
    platform reports is ever silently dropped."""
    by_name = {c.name: c for c in currents}
    placed: set[str] = set()
    groups: list[dict] = []
    for g in GROUPS:
        items = [by_name[n] for n in member_names(g, set(by_name))]
        placed.update(c.name for c in items)
        if items:
            groups.append({"key": g.key, "name": g.name, "blurb": g.blurb, "core": g.core,
                           "third_party": g.third_party, "services": items})
    leftovers = [c for c in currents if c.name not in placed]
    if leftovers:
        groups.append({"key": "other", "name": "Other", "blurb": "Reported by the platform",
                       "core": False, "third_party": False, "services": leftovers})
    return groups


def build_components(currents: list[CurrentService], windows: dict | None = None) -> list[dict]:
    """The page's component list: each customer-facing group with its
    rolled-up status, its uptime per window, and the checks behind it."""
    windows = windows if windows is not None else uptime_windows()
    out: list[dict] = []
    for g in group_currents(currents):
        checks = []
        for c in g["services"]:
            svc = service_info(c.name)
            checks.append({
                "name": c.name,
                "label": svc.label if svc else c.name,
                "what": svc.what if svc else "",
                "how": HOW_LABEL.get(svc.how, "") if svc else "Reported by the platform",
                "advisory": bool(svc and svc.advisory),
                "status": c.status,
                "note": _public_note(c),
                "response_ms": c.response_ms,
                "checked_at": c.checked_at,
                "uptime": {key: (windows.get(c.name) or {}).get(key) for key, _ in UPTIME_WINDOWS},
            })
        members = [c for c in g["services"]]
        out.append({
            "key": g["key"],
            "name": g["name"],
            "blurb": g["blurb"],
            "core": g["core"],
            "third_party": g["third_party"],
            "status": _rollup(members),
            "uptime": {key: _group_window(members, windows, key) for key, _ in UPTIME_WINDOWS},
            "checks": checks,
        })
    return out


def _public_note(c: CurrentService) -> str | None:
    """A short public explanation for a check that is not plainly fine.
    Internal error text (hostnames, exception strings) is never passed on;
    only messages we wrote for the public are."""
    if c.name == "Discord" and c.error and c.status in ("degraded", "down"):
        return c.error
    if c.status == "unknown":
        if c.error == "monitor offline":
            return "Our monitor is offline, so this was not checked."
        if c.error and c.error.startswith("not measured"):
            return "Not checked while the website is unreachable."
        return "No recent check."
    if c.name == "SSL Certificate" and c.status == "degraded":
        return "Renewal is due soon."
    return None


# ── Uptime ────────────────────────────────────────────────────────────────

def _pct(up: int, down: int) -> float | None:
    """Uptime as a share of COMPLETED checks. Checks that could not be made
    ("unknown") are left out entirely: they are neither up nor down."""
    total = up + down
    if total <= 0:
        return None
    return round(up / total * 100.0, 3)


def uptime_windows() -> dict[str, dict[str, dict | None]]:
    """Per-service uptime over each window in UPTIME_WINDOWS.

    Returns {service: {"24h": {"pct", "checks", "failed"} | None, ...}}.
    24 hours is a rolling window over the raw checks; the longer windows sum
    whole UTC days from daily_uptime (today included).
    """
    now = datetime.now(timezone.utc)
    out: dict[str, dict[str, dict | None]] = {}

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT service_name,
                   SUM(CASE WHEN status IN ('operational','degraded') THEN 1 ELSE 0 END) AS up,
                   SUM(CASE WHEN status='down' THEN 1 ELSE 0 END) AS down
            FROM probe_results
            WHERE checked_at >= ?
            GROUP BY service_name
            """,
            (_to_iso(now - timedelta(hours=24)),),
        ).fetchall()
        for r in rows:
            name = r["service_name"]
            if name.startswith("__"):
                continue
            up, down = int(r["up"] or 0), int(r["down"] or 0)
            pct = _pct(up, down)
            out.setdefault(name, {})["24h"] = (
                {"pct": pct, "checks": up + down, "failed": down} if pct is not None else None)

        for key, days in UPTIME_WINDOWS:
            if key == "24h":
                continue
            cutoff = (now - timedelta(days=days - 1)).date().isoformat()
            rows = conn.execute(
                """
                SELECT service_name, SUM(total_checks) AS total, SUM(failed_checks) AS failed,
                       COUNT(*) AS days
                FROM daily_uptime WHERE day >= ? GROUP BY service_name
                """,
                (cutoff,),
            ).fetchall()
            for r in rows:
                name = r["service_name"]
                if name.startswith("__"):
                    continue
                total, failed = int(r["total"] or 0), int(r["failed"] or 0)
                pct = _pct(total - failed, failed)
                out.setdefault(name, {})[key] = (
                    {"pct": pct, "checks": total, "failed": failed, "days": int(r["days"] or 0)}
                    if pct is not None else None)

    for windows in out.values():
        for key, _ in UPTIME_WINDOWS:
            windows.setdefault(key, None)
    return out


def _group_window(members: list[CurrentService], windows: dict, key: str) -> dict | None:
    """A component's uptime for one window: that of its weakest check.
    A component is only as available as its least available part, and this
    never overstates. Diagnostics (domain lookup, certificate) are left out."""
    worst: dict | None = None
    for c in members:
        if c.name in ADVISORY_SERVICES:
            continue
        w = (windows.get(c.name) or {}).get(key)
        if not w or w.get("pct") is None:
            continue
        if worst is None or w["pct"] < worst["pct"]:
            worst = w
    return dict(worst) if worst else None


def headline_uptime(components: list[dict]) -> dict[str, float | None]:
    """One figure per window for the page header: the mean uptime of the
    core customer-facing components (third-party and developer/support
    tooling excluded)."""
    out: dict[str, float | None] = {}
    for key, _ in UPTIME_WINDOWS:
        vals = [c["uptime"][key]["pct"] for c in components
                if c.get("core") and c["uptime"].get(key) and c["uptime"][key].get("pct") is not None]
        out[key] = round(sum(vals) / len(vals), 3) if vals else None
    return out


def data_since() -> str | None:
    """First UTC day we hold any uptime data for. Lets the page say how far
    back a figure really reaches instead of implying a full window."""
    with db.connect() as conn:
        row = conn.execute("SELECT MIN(day) AS d FROM daily_uptime").fetchone()
    return row["d"] if row and row["d"] else None


def daily_uptime_series(days: int = 90) -> dict:
    """Per-service and per-component daily uptime for the timeline.
    Today's row is aggregated live so the last cell is never stale."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days - 1)).date().isoformat()
    with db.connect() as conn:
        agg_rows = conn.execute(
            """
            SELECT service_name, day, uptime_pct, total_checks, failed_checks
            FROM daily_uptime
            WHERE day >= ?
            ORDER BY day ASC
            """,
            (cutoff,),
        ).fetchall()

    per_service: dict[str, dict[str, dict]] = {}
    for r in agg_rows:
        if r["service_name"].startswith("__") or not r["total_checks"]:
            continue
        per_service.setdefault(r["service_name"], {})[r["day"]] = {
            "day": r["day"],
            "uptime_pct": r["uptime_pct"],
            "total_checks": r["total_checks"],
            "failed_checks": r["failed_checks"],
        }

    today = now.date().isoformat()
    for name, live in _live_uptime_for_day(today).items():
        per_service.setdefault(name, {})[today] = {
            "day": today,
            "uptime_pct": live["pct"],
            "total_checks": live["total"],
            "failed_checks": live["failed"],
        }

    series = {name: [by_day[d] for d in sorted(by_day)] for name, by_day in per_service.items()}

    groups: dict[str, list[dict]] = {}
    for g in GROUPS:
        by_day: dict[str, dict] = {}
        for name in member_names(g, set(per_service)):
            if name in ADVISORY_SERVICES:
                continue
            for day, entry in per_service.get(name, {}).items():
                cur = by_day.get(day)
                if cur is None or entry["uptime_pct"] < cur["uptime_pct"]:
                    by_day[day] = {**entry, "service": name}
        if by_day:
            groups[g.key] = [by_day[d] for d in sorted(by_day)]

    return {"days": days, "series": series, "groups": groups,
            "interval_seconds": get_settings().probe_interval_seconds}


# ── Incidents ─────────────────────────────────────────────────────────────

_INCIDENT_MIN_DURATION_MIN = 2  # shorter interruptions count toward uptime but are not listed
# Failures separated by less than this much normal running are one "on and
# off" event, the way people lived it, rather than several short ones.
_EVENT_MERGE_GAP = timedelta(minutes=15)
# An open incident whose service has gone "no data" is closed at its last
# confirmed-down check once this long has passed without a fresh one.
_INCIDENT_NO_DATA_CLOSE = timedelta(minutes=30)


def incidents_recent(days: int = 7, max_count: int = 20) -> list[dict]:
    """Raw per-service incident rows (blips under two minutes filtered out).
    The public pages use incident_events(); this stays for the JSON API."""
    cutoff = _to_iso(datetime.now(timezone.utc) - timedelta(days=days))
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, service_name, started_at, ended_at, duration_min, resolved, cause, cause_at
            FROM incidents
            WHERE started_at >= ?
              AND (resolved = 0 OR duration_min IS NULL OR duration_min >= ?)
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (cutoff, _INCIDENT_MIN_DURATION_MIN, max_count),
        ).fetchall()
    return [dict(r) for r in rows]


def incident_events(days: int = 7, max_count: int = 20) -> list[dict]:
    """Incidents as people experienced them: one entry per event.

    The table holds one row per service, so a single six-minute website
    problem used to be listed as up to fourteen separate "incidents". Rows
    that overlap in time, or sit within a quarter of an hour of each other,
    are one event, shown once with everything it affected.

    `duration_min` is the event from first failure to last recovery;
    `down_min` is how much of that was actually spent down. They differ for
    an on-and-off problem, and the page says so instead of presenting an
    hour of flakiness as an hour of outage.
    """
    now = datetime.now(timezone.utc)
    cutoff = _to_iso(now - timedelta(days=days))
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, service_name, started_at, ended_at, duration_min, resolved, cause, cause_at
            FROM incidents
            WHERE started_at >= ?
            ORDER BY started_at ASC, id ASC
            """,
            (cutoff,),
        ).fetchall()

    clusters: list[dict] = []
    for r in rows:
        name = r["service_name"]
        if name.startswith("__") or _is_third_party(name):
            continue
        try:
            start = _parse_iso(r["started_at"])
            if not r["resolved"]:
                end = None
            elif r["ended_at"]:
                end = _parse_iso(r["ended_at"])
            else:
                # Resolved but never stamped with an end: fall back to the
                # recorded duration rather than treating it as still open.
                end = start + timedelta(minutes=int(r["duration_min"] or 0))
        except Exception:
            continue
        reach = end or now
        cur = clusters[-1] if clusters else None
        if cur is not None and start <= cur["reach"] + _EVENT_MERGE_GAP:
            cur["rows"].append(r)
            cur["spans"].append((start, reach))
            cur["reach"] = max(cur["reach"], reach)
            cur["open"] = cur["open"] or end is None
        else:
            clusters.append({"rows": [r], "spans": [(start, reach)], "start": start,
                             "reach": reach, "open": end is None})

    events: list[dict] = []
    for cl in clusters:
        members = cl["rows"]
        resolved = not cl["open"]
        duration = max(1, int((cl["reach"] - cl["start"]).total_seconds() // 60))
        down = max(1, int(_union_seconds(cl["spans"]) // 60))
        causes = sorted((m for m in members if m["cause"]), key=lambda m: m["cause_at"] or "", reverse=True)
        cause = causes[0]["cause"] if causes else None
        if resolved and not cause and down < _INCIDENT_MIN_DURATION_MIN:
            continue
        names = {m["service_name"] for m in members}
        services = [n for n in SERVICE_ORDER if n in names] + sorted(names - set(SERVICE_ORDER))
        groups = [g for g in GROUPS if any(group_of(n) is g for n in names)]
        events.append({
            "id": min(int(m["id"]) for m in members),
            "ids": sorted(int(m["id"]) for m in members),
            "started_at": _to_iso(cl["start"]),
            "ended_at": _to_iso(cl["reach"]) if resolved else None,
            "resolved": resolved,
            "duration_min": duration,
            "down_min": down,
            # Clearly less downtime than elapsed time: it came and went.
            "intermittent": resolved and duration - down >= 3,
            "services": services,
            "service_labels": [service_label(n) for n in services],
            "groups": [{"key": g.key, "name": g.name} for g in groups],
            "title": _event_title(groups, services),
            "cause": cause,
            "cause_at": causes[0]["cause_at"] if causes else None,
        })

    events.sort(key=lambda e: e["started_at"], reverse=True)
    return events[:max_count]


def _union_seconds(spans: list[tuple[datetime, datetime]]) -> float:
    """Total time covered by a set of possibly overlapping intervals."""
    total = 0.0
    cur_start: datetime | None = None
    cur_end: datetime | None = None
    for start, end in sorted(spans):
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += (cur_end - cur_start).total_seconds()
            cur_start, cur_end = start, end
        elif end > cur_end:
            cur_end = end
    if cur_end is not None:
        total += (cur_end - cur_start).total_seconds()
    return total


def _event_title(groups: list[Group], services: list[str]) -> str:
    names = [g.name for g in groups]
    if not names:
        return (", ".join(service_label(s) for s in services[:2]) or "Service") + " disruption"
    if len(names) == 1:
        return f"{names[0]} disruption"
    if len(names) <= 3:
        return "Disruption: " + ", ".join(names)
    return f"Disruption across {len(names)} components"


def event_duration_text(event: dict) -> str:
    """How long an event lasted, in words."""
    if not event["resolved"]:
        return f"{format_duration(event['duration_min'])} so far"
    if event.get("intermittent"):
        return (f"on and off for {format_duration(event['duration_min'])}, "
                f"{format_duration(event['down_min'])} of it down")
    return f"lasted {format_duration(event['duration_min'])}"


def format_duration(minutes: int | None) -> str:
    if not minutes:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, rem = divmod(int(minutes), 60)
    return f"{hours} h {rem} min" if rem else f"{hours} h"


# ── Live numbers ──────────────────────────────────────────────────────────

def newest_probe_at() -> datetime | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT checked_at AS m FROM probe_results ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row or not row["m"]:
        return None
    try:
        return _parse_iso(row["m"])
    except Exception:
        return None


def monitor_state() -> dict:
    """Whether the monitor's own connection was working on its last cycle,
    and since when it has been offline if not."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, checked_at FROM probe_results WHERE service_name='__monitor__' "
            "ORDER BY checked_at DESC LIMIT 720"
        ).fetchall()
    if not rows:
        return {"online": None, "offline_since": None}
    if rows[0]["status"] != "down":
        return {"online": True, "offline_since": None}
    since = rows[0]["checked_at"]
    for r in rows:
        if r["status"] != "down":
            break
        since = r["checked_at"]
    return {"online": False, "offline_since": since}


def _percentile(sorted_vals: list[int], q: float) -> int:
    """Nearest-rank percentile over an already-sorted list."""
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1))))
    return int(sorted_vals[idx])


# Only checks whose timing is a real measurement of the thing itself. The
# platform-reported rows carry the duration of a heartbeat lookup, which is
# not a response time and used to be charted as one.
RESPONSE_TIME_SERVICES = ("Public Site",)


def response_time_series(hours: int = 24, max_points: int = 144, services: tuple[str, ...] | None = None) -> dict:
    """Response-time percentiles for the chart. Samples are bucketed into
    ~max_points equal time windows and each bucket reports p50/p95."""
    services = services or RESPONSE_TIME_SERVICES
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    placeholders = ",".join("?" for _ in services)
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT service_name, response_ms, checked_at
            FROM probe_results
            WHERE checked_at >= ? AND response_ms IS NOT NULL
              AND status IN ('operational','degraded')
              AND service_name IN ({placeholders})
            ORDER BY checked_at ASC
            """,
            (_to_iso(cutoff), *services),
        ).fetchall()

    bucket_sec = max(60, int(hours * 3600 / max(1, max_points)))
    grouped: dict[str, dict[int, list[int]]] = {}
    for r in rows:
        try:
            epoch = int(_parse_iso(r["checked_at"]).timestamp())
        except Exception:
            continue
        bucket = (epoch // bucket_sec) * bucket_sec
        grouped.setdefault(r["service_name"], {}).setdefault(bucket, []).append(int(r["response_ms"]))

    series: dict[str, list[dict]] = {}
    for name, buckets in grouped.items():
        points = []
        for bucket in sorted(buckets):
            vals = sorted(buckets[bucket])
            points.append({
                "t": _to_iso(datetime.fromtimestamp(bucket, tz=timezone.utc)),
                "p50": _percentile(vals, 0.50),
                "p95": _percentile(vals, 0.95),
                "n": len(vals),
            })
        series[name] = points
    return {"hours": hours, "bucket_seconds": bucket_sec, "series": series}


def shard_summary() -> dict:
    """Group shard_snapshot rows into clusters with per-cluster counts."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT cluster_idx, shard_id, status, latency_ms, guild_count, fetched_at "
            "FROM shard_snapshot ORDER BY cluster_idx, shard_id"
        ).fetchall()
    totals = {"shards": 0, "guilds": 0, "online": 0, "degraded": 0, "down": 0, "unknown": 0}
    if not rows:
        return {"clusters": [], "totals": totals, "latency_ms": None}
    clusters: dict[int, dict] = {}
    latencies: list[int] = []
    for r in rows:
        c = clusters.setdefault(int(r["cluster_idx"]), {"cluster_idx": int(r["cluster_idx"]), "shards": []})
        c["shards"].append({
            "shard_id": int(r["shard_id"]),
            "status": r["status"],
            "latency_ms": r["latency_ms"],
            "guild_count": r["guild_count"],
        })
        totals["shards"] += 1
        totals["guilds"] += r["guild_count"] or 0
        if r["status"] == "operational":
            totals["online"] += 1
            if r["latency_ms"] is not None:
                latencies.append(int(r["latency_ms"]))
        elif r["status"] == "degraded":
            totals["degraded"] += 1
        elif r["status"] == "down":
            totals["down"] += 1
        else:
            totals["unknown"] += 1
    latency = round(sum(latencies) / len(latencies)) if latencies else None
    return {"clusters": list(clusters.values()), "totals": totals, "latency_ms": latency}


def sla_summary(target_pct: float, days: int = 30) -> dict:
    """Average uptime over the last N days across our own services —
    external dependencies and diagnostics (SLA_EXCLUDED_SERVICES) don't
    count toward the number we hold ourselves to."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    placeholders = ",".join("?" for _ in SLA_EXCLUDED_SERVICES)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT AVG(uptime_pct) AS avg_pct FROM daily_uptime "
            f"WHERE day >= ? AND total_checks > 0 AND service_name NOT IN ({placeholders})",
            (cutoff, *SLA_EXCLUDED_SERVICES),
        ).fetchone()
    actual = float(rows["avg_pct"]) if rows and rows["avg_pct"] is not None else 100.0
    return {
        "target_pct": target_pct,
        "actual_pct": round(actual, 3),
        "days": days,
        "below_target": actual < target_pct,
        "at_risk": actual < (target_pct - 0.5),
    }


# ── Roll-up (called once per probe cycle) ─────────────────────────────────

def roll_up_after_probe() -> None:
    """Updates today's daily_uptime rows and opens/closes incident records
    based on the latest per-service status."""
    today = datetime.now(timezone.utc).date().isoformat()
    live = _live_uptime_for_day(today)
    with db.connect() as conn:
        for name, payload in live.items():
            conn.execute(
                """
                INSERT INTO daily_uptime(service_name, day, uptime_pct, total_checks, failed_checks)
                VALUES (?,?,?,?,?)
                ON CONFLICT(service_name, day) DO UPDATE SET
                  uptime_pct=excluded.uptime_pct,
                  total_checks=excluded.total_checks,
                  failed_checks=excluded.failed_checks
                """,
                (name, today, payload["pct"], payload["total"], payload["failed"]),
            )

    _update_incidents()


def _update_incidents() -> None:
    """Open an incident when a service is confirmed down; close it when a
    later check confirms it is back.

    "No data" neither opens nor closes anything: we cannot tell. An open
    incident left without fresh evidence for half an hour is closed at its
    last confirmed-down check rather than left running on an assumption.
    """
    now = datetime.now(timezone.utc)
    streak_cutoff = _to_iso(now - timedelta(minutes=10))
    with db.connect() as conn:
        currents = _recent_latest_rows(conn)
        latest = {row["service_name"]: row for row in currents}
        open_rows = conn.execute(
            "SELECT id, service_name, started_at FROM incidents WHERE resolved=0"
        ).fetchall()
        open_by_name = {r["service_name"]: r for r in open_rows}

        for name, row in latest.items():
            if name.startswith("__") or _is_third_party(name):
                continue
            status, checked_at = row["status"], row["checked_at"]
            open_row = open_by_name.get(name)
            if status == "down":
                if open_row is None:
                    streak_started = _streak_start(conn, name, "down", streak_cutoff) or checked_at
                    conn.execute(
                        "INSERT INTO incidents(service_name, started_at, resolved) VALUES (?,?,0)",
                        (name, streak_started),
                    )
            elif status in ("operational", "degraded") and open_row is not None:
                _close_incident(conn, open_row, checked_at)

        # Open incidents with no fresh verdict: close at the last confirmed
        # down once the evidence has gone cold.
        for name, open_row in open_by_name.items():
            row = latest.get(name)
            if row is not None and row["status"] in _VERDICTS:
                continue
            last_down = conn.execute(
                "SELECT checked_at FROM probe_results WHERE service_name=? AND status='down' "
                "ORDER BY checked_at DESC LIMIT 1",
                (name,),
            ).fetchone()
            last_down_at = last_down["checked_at"] if last_down else open_row["started_at"]
            try:
                if now - _parse_iso(last_down_at) >= _INCIDENT_NO_DATA_CLOSE:
                    _close_incident(conn, open_row, last_down_at)
            except Exception:
                continue


def _close_incident(conn, open_row, ended_at: str) -> None:
    started = _parse_iso(open_row["started_at"])
    ended = _parse_iso(ended_at) if ended_at else datetime.now(timezone.utc)
    duration = max(1, int((ended - started).total_seconds() // 60))
    conn.execute(
        "UPDATE incidents SET resolved=1, ended_at=?, duration_min=? WHERE id=?",
        (_to_iso(ended), duration, open_row["id"]),
    )


def _streak_start(conn, service_name: str, status: str, cutoff_iso: str) -> str | None:
    rows = conn.execute(
        "SELECT status, checked_at FROM probe_results "
        "WHERE service_name=? AND checked_at >= ? ORDER BY checked_at DESC",
        (service_name, cutoff_iso),
    ).fetchall()
    last_match = None
    for r in rows:
        if r["status"] == status:
            last_match = r["checked_at"]
        else:
            break
    return last_match


def _live_uptime_for_day(day_iso: str) -> dict[str, dict]:
    """Aggregate one UTC day's probe_results into per-service uptime.
    Only completed checks count: a service with nothing but "no data" that
    day gets no entry at all, rather than a made-up 100%."""
    day_start = f"{day_iso}T00:00:00.000Z"
    next_day = (datetime.fromisoformat(day_iso) + timedelta(days=1)).date().isoformat()
    day_end = f"{next_day}T00:00:00.000Z"
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT service_name,
                   SUM(CASE WHEN status IN ('operational','degraded') THEN 1 ELSE 0 END) AS up,
                   SUM(CASE WHEN status='down' THEN 1 ELSE 0 END) AS failed
            FROM probe_results
            WHERE checked_at >= ? AND checked_at < ?
            GROUP BY service_name
            """,
            (day_start, day_end),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        name = r["service_name"]
        if name.startswith("__"):
            continue
        up, failed = int(r["up"] or 0), int(r["failed"] or 0)
        pct = _pct(up, failed)
        if pct is None:
            continue
        out[name] = {"pct": pct, "total": up + failed, "failed": failed}
    return out


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
