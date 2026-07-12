from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import db

SERVICE_ORDER = [
    "Public Site",
    "Dashboard",
    "Gateway",
    "Plugin Runner",
    "Orchestrator",
    "Bot",
    "Discord API",
    "Bot Worker",
    "Analytics",
    "Sandbox",
    "WebSocket Broker",
    "Image Service",
    "Database",
    "Cache",
    "DNS",
    "SSL Certificate",
]

# Services that may legitimately never report (optional stack members or
# probes that need config, e.g. DISCORD_BOT_TOKEN). They're hidden until
# the first real data point instead of showing a permanent gray
# "no data yet" row.
OPTIONAL_SERVICES = {"Discord API", "WebSocket Broker", "Image Service"}

# External dependencies and meta-checks excluded from OUR uptime SLA —
# Discord's API health isn't our availability, and the hourly SSL
# check's cadence would skew the average.
SLA_EXCLUDED_SERVICES = ("Discord API", "SSL Certificate")

PROXY_STALE_AFTER_SECONDS = 300  # 5 minutes — age out proxy data when /status/api stops returning

# Display grouping for the public status page. Services not listed here fall
# into a trailing "Other" group so nothing is ever silently dropped.
SERVICE_GROUPS: list[tuple[str, list[str]]] = [
    ("Website & Dashboard", ["Public Site", "Dashboard"]),
    ("Bot & Gateway", ["Gateway", "Bot", "Bot Worker", "Orchestrator", "Discord API"]),
    ("Plugins", ["Plugin Runner", "Sandbox", "WebSocket Broker"]),
    ("Data & Infrastructure", ["Analytics", "Image Service", "Database", "Cache", "DNS", "SSL Certificate"]),
]


def group_currents(currents: "list[CurrentService]") -> list[dict]:
    """Bucket the flat current-service list into display groups (SERVICE_GROUPS
    order), appending any ungrouped services as a final 'Other' group."""
    by_name = {c.name: c for c in currents}
    placed: set[str] = set()
    groups: list[dict] = []
    for title, names in SERVICE_GROUPS:
        items = []
        for n in names:
            c = by_name.get(n)
            if c is not None:
                items.append(c)
                placed.add(n)
        if items:
            groups.append({"name": title, "services": items})
    leftovers = [c for c in currents if c.name not in placed]
    if leftovers:
        groups.append({"name": "Other", "services": leftovers})
    return groups


@dataclass
class CurrentService:
    name: str
    status: str
    response_ms: int | None
    checked_at: str
    error: str | None


def latest_per_service() -> list[CurrentService]:
    """Return the latest probe per service, ordered by SERVICE_ORDER. Services
    that haven't been seen recently are aged out to 'unknown'."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT service_name, status, response_ms, checked_at, error, source
            FROM probe_results
            WHERE id IN (
              SELECT MAX(id) FROM probe_results GROUP BY service_name
            )
            """
        ).fetchall()

    by_name = {row["service_name"]: row for row in rows}
    now = datetime.now(timezone.utc)
    out: list[CurrentService] = []
    seen: set[str] = set()

    for name in SERVICE_ORDER:
        row = by_name.get(name)
        if row is None:
            if name in OPTIONAL_SERVICES:
                continue  # never reported — hide instead of a permanent gray row
            out.append(CurrentService(name=name, status="unknown", response_ms=None,
                                       checked_at="", error="no data yet"))
            continue
        seen.add(name)
        status = row["status"]
        if row["source"] == "proxy":
            try:
                age = (now - _parse_iso(row["checked_at"])).total_seconds()
                if age > PROXY_STALE_AFTER_SECONDS:
                    status = "unknown"
            except Exception:
                status = "unknown"
        out.append(CurrentService(
            name=name,
            status=status,
            response_ms=row["response_ms"],
            checked_at=row["checked_at"],
            error=row["error"],
        ))

    for name, row in by_name.items():
        if name in seen or name.startswith("__"):
            continue
        out.append(CurrentService(
            name=name,
            status=row["status"],
            response_ms=row["response_ms"],
            checked_at=row["checked_at"],
            error=row["error"],
        ))

    return out


def seen_proxy_services() -> set[str]:
    """Service names that have ever reported via the /status/api proxy.
    Used by the scheduler to decide which OPTIONAL services join the
    down/unknown fan-out when the platform is unreachable."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT service_name FROM probe_results WHERE source='proxy'"
        ).fetchall()
    return {r["service_name"] for r in rows}


def overall_status(currents: list[CurrentService]) -> str:
    """Reduce per-service statuses to a single overall verdict."""
    statuses = {s.status for s in currents if s.name != "SSL Certificate" or s.status != "unknown"}
    if not statuses or statuses == {"unknown"}:
        return "unknown"
    if "down" in statuses:
        critical = {"Public Site", "Database", "DNS"}
        if any(s.status == "down" and s.name in critical for s in currents):
            return "outage"
        return "partial_outage"
    if "degraded" in statuses:
        return "degraded"
    return "operational"


def newest_probe_at() -> datetime | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT MAX(checked_at) AS m FROM probe_results"
        ).fetchone()
    if not row or not row["m"]:
        return None
    try:
        return _parse_iso(row["m"])
    except Exception:
        return None


def _percentile(sorted_vals: list[int], q: float) -> int:
    """Nearest-rank percentile over an already-sorted list."""
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1))))
    return int(sorted_vals[idx])


def response_time_series(hours: int = 6, max_points: int = 120) -> dict:
    """Per-service response-time percentiles for the chart. Samples are
    bucketed into ~max_points equal time windows and each bucket reports
    p50/p95 — a p95 band shows creeping degradation long before the raw
    median (or the 2s degraded threshold) moves."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_str = _to_iso(cutoff)
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT service_name, response_ms, checked_at
            FROM probe_results
            WHERE checked_at >= ? AND response_ms IS NOT NULL AND status <> 'unknown'
            ORDER BY checked_at ASC
            """,
            (cutoff_str,),
        ).fetchall()

    bucket_sec = max(60, int(hours * 3600 / max(1, max_points)))
    # service → bucket epoch → list of response_ms
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


def daily_uptime_series(days: int = 90) -> dict:
    """Per-service per-day uptime% from the daily_uptime table. Falls back
    to live aggregation for days that haven't been rolled up yet (today)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with db.connect() as conn:
        agg_rows = conn.execute(
            """
            SELECT service_name, day, uptime_pct, total_checks
            FROM daily_uptime
            WHERE day >= ?
            ORDER BY day ASC
            """,
            (cutoff,),
        ).fetchall()

    series: dict[str, list[dict]] = {}
    for r in agg_rows:
        series.setdefault(r["service_name"], []).append({
            "day": r["day"],
            "uptime_pct": r["uptime_pct"],
            "total_checks": r["total_checks"],
        })

    today = datetime.now(timezone.utc).date().isoformat()
    today_rows = _live_uptime_for_day(today)
    for name, pct_total in today_rows.items():
        existing = next((d for d in series.get(name, []) if d["day"] == today), None)
        if existing:
            existing["uptime_pct"] = pct_total["pct"]
            existing["total_checks"] = pct_total["total"]
        else:
            series.setdefault(name, []).append({
                "day": today,
                "uptime_pct": pct_total["pct"],
                "total_checks": pct_total["total"],
            })
    return {"days": days, "series": series}


_INCIDENT_MIN_DURATION_MIN = 2  # filter single-probe blips (network jitter, restart 502s)


def incidents_recent(days: int = 7, max_count: int = 20) -> list[dict]:
    """Recent incidents, oldest blips filtered out. A 1-minute incident
    typically means a single failed probe followed by a successful one
    (Caddy 502 during a dashboard restart, network jitter, etc.) — not a
    real outage worth surfacing on the page. Mirrors the platform's own
    >=2 min threshold at api_status.status_incidents()."""
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


def shard_summary() -> dict:
    """Group shard_snapshot rows into clusters with per-cluster counts."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT cluster_idx, shard_id, status, latency_ms, guild_count, fetched_at "
            "FROM shard_snapshot ORDER BY cluster_idx, shard_id"
        ).fetchall()
    if not rows:
        return {"clusters": [], "totals": {"shards": 0, "guilds": 0, "online": 0, "down": 0}}
    clusters: dict[int, dict] = {}
    for r in rows:
        c = clusters.setdefault(int(r["cluster_idx"]), {"cluster_idx": int(r["cluster_idx"]), "shards": []})
        c["shards"].append({
            "shard_id": int(r["shard_id"]),
            "status": r["status"],
            "latency_ms": r["latency_ms"],
            "guild_count": r["guild_count"],
        })
    totals = {"shards": 0, "guilds": 0, "online": 0, "down": 0}
    for c in clusters.values():
        for s in c["shards"]:
            totals["shards"] += 1
            totals["guilds"] += s.get("guild_count") or 0
            if s.get("status") == "operational":
                totals["online"] += 1
            elif s.get("status") == "down":
                totals["down"] += 1
    return {"clusters": list(clusters.values()), "totals": totals}


def sla_summary(target_pct: float, days: int = 30) -> dict:
    """Average uptime over the last N days across our own services —
    external dependencies and meta-checks (SLA_EXCLUDED_SERVICES) don't
    count toward the SLA number we publish."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    placeholders = ",".join("?" for _ in SLA_EXCLUDED_SERVICES)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT AVG(uptime_pct) AS avg_pct FROM daily_uptime "
            f"WHERE day >= ? AND service_name NOT IN ({placeholders})",
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


def roll_up_after_probe() -> None:
    """Called once per probe cycle. Updates today's daily_uptime row and
    opens/closes incident records based on the latest per-service status."""
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
    """For each service with an active down/degraded streak in the last
    few minutes, ensure an open incident exists. For services that
    recovered, close any open incident."""
    cutoff = _to_iso(datetime.now(timezone.utc) - timedelta(minutes=10))
    with db.connect() as conn:
        currents = conn.execute(
            """
            SELECT service_name, status, checked_at
            FROM probe_results
            WHERE id IN (SELECT MAX(id) FROM probe_results GROUP BY service_name)
            """
        ).fetchall()
        for row in currents:
            name = row["service_name"]
            if name.startswith("__"):
                continue
            status = row["status"]
            checked_at = row["checked_at"]
            open_row = conn.execute(
                "SELECT id, started_at FROM incidents WHERE service_name=? AND resolved=0",
                (name,),
            ).fetchone()
            if status == "down":
                if open_row is None:
                    streak_started = _streak_start(conn, name, "down", cutoff) or checked_at
                    conn.execute(
                        "INSERT INTO incidents(service_name, started_at, resolved) VALUES (?,?,0)",
                        (name, streak_started),
                    )
            else:
                if open_row is not None:
                    started = _parse_iso(open_row["started_at"])
                    ended = _parse_iso(checked_at) if checked_at else datetime.now(timezone.utc)
                    duration = max(1, int((ended - started).total_seconds() // 60))
                    conn.execute(
                        "UPDATE incidents SET resolved=1, ended_at=?, duration_min=? WHERE id=?",
                        (checked_at, duration, open_row["id"]),
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
    """Aggregate today's probe_results into per-service uptime%."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT service_name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status='down' THEN 1 ELSE 0 END) AS failed
            FROM probe_results
            WHERE substr(checked_at, 1, 10) = ?
            GROUP BY service_name
            """,
            (day_iso,),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        name = r["service_name"]
        if name.startswith("__"):
            continue
        total = int(r["total"] or 0)
        failed = int(r["failed"] or 0)
        pct = 100.0 if total == 0 else round(((total - failed) / total) * 100.0, 3)
        out[name] = {"pct": pct, "total": total, "failed": failed}
    return out


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
