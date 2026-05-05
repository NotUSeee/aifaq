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
    "Bot Worker",
    "Analytics",
    "Sandbox",
    "Database",
    "Cache",
    "DNS",
    "SSL Certificate",
]

PROXY_STALE_AFTER_SECONDS = 300  # 5 minutes — age out proxy data when /status/api stops returning


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


def response_time_series(hours: int = 6, max_points: int = 120) -> dict:
    """Per-service response_ms timeseries for the chart. Downsamples by
    bucketing into max_points equal-width windows."""
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

    series: dict[str, list[dict]] = {}
    for r in rows:
        series.setdefault(r["service_name"], []).append({
            "t": r["checked_at"],
            "ms": r["response_ms"],
        })

    if max_points and rows:
        for name, points in list(series.items()):
            if len(points) > max_points:
                step = len(points) // max_points
                series[name] = points[::step][:max_points]
    return {"hours": hours, "series": series}


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


def incidents_recent(days: int = 7, max_count: int = 20) -> list[dict]:
    cutoff = _to_iso(datetime.now(timezone.utc) - timedelta(days=days))
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, service_name, started_at, ended_at, duration_min, resolved
            FROM incidents
            WHERE started_at >= ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (cutoff, max_count),
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
    """Average uptime across SERVICE_ORDER over the last N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT AVG(uptime_pct) AS avg_pct FROM daily_uptime WHERE day >= ?",
            (cutoff,),
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
