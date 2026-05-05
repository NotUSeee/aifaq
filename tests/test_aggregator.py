from __future__ import annotations

from datetime import datetime, timedelta, timezone

from status_service import db
from status_service.aggregator import (
    SERVICE_ORDER,
    latest_per_service,
    overall_status,
    roll_up_after_probe,
    sla_summary,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _insert_probe(service_name: str, status: str, source: str = "external", at: datetime | None = None):
    when = at or datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO probe_results(service_name,status,response_ms,http_status,error,source,checked_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (service_name, status, 50, 200 if status != "down" else None, None, source, _iso(when)),
        )


def test_latest_per_service_returns_service_order_first():
    _insert_probe("Public Site", "operational")
    _insert_probe("Bot", "operational", source="proxy")
    rows = latest_per_service()
    names = [r.name for r in rows]
    for s in SERVICE_ORDER:
        assert s in names


def test_latest_per_service_ages_out_proxy_after_5min():
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    _insert_probe("Bot", "operational", source="proxy", at=old)
    rows = latest_per_service()
    bot = next(r for r in rows if r.name == "Bot")
    assert bot.status == "unknown"


def test_latest_per_service_keeps_recent_proxy_data():
    recent = datetime.now(timezone.utc) - timedelta(minutes=2)
    _insert_probe("Bot", "operational", source="proxy", at=recent)
    rows = latest_per_service()
    bot = next(r for r in rows if r.name == "Bot")
    assert bot.status == "operational"


def test_fresh_unknown_overrides_stale_operational():
    """When the prober writes `unknown` for an internal service this
    cycle (because the public site is unreachable), the page must
    reflect that immediately — not show the stale `operational` row
    written before the outage."""
    earlier = datetime.now(timezone.utc) - timedelta(minutes=1)
    _insert_probe("Bot", "operational", source="proxy", at=earlier)
    _insert_probe("Bot", "unknown", source="proxy")
    rows = latest_per_service()
    bot = next(r for r in rows if r.name == "Bot")
    assert bot.status == "unknown"


def test_mark_shards_unreachable_flips_all_rows_down():
    """Regression: shard_snapshot used to stay at 'operational' forever
    when /status/api/shards started failing. Now the prober must flip
    all rows to 'down' on probe failure so the page stops showing stale
    online clusters."""
    from status_service.config import get_settings
    from status_service.scheduler import Scheduler

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO shard_snapshot(cluster_idx,shard_id,status,latency_ms,guild_count,fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (0, 0, "operational", 50, 1234, _iso(datetime.now(timezone.utc))),
        )
        conn.execute(
            "INSERT INTO shard_snapshot(cluster_idx,shard_id,status,latency_ms,guild_count,fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (0, 1, "operational", 60, 5678, _iso(datetime.now(timezone.utc))),
        )

    sched = Scheduler(get_settings())
    try:
        sched._mark_shards_unreachable()
    finally:
        # Don't await aclose() in a sync test — just close the http client.
        pass

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, guild_count FROM shard_snapshot ORDER BY shard_id"
        ).fetchall()
    assert all(r["status"] == "down" for r in rows)
    # Guild counts preserved so cluster topology stays visible.
    assert [r["guild_count"] for r in rows] == [1234, 5678]


def test_overall_all_operational():
    rows = latest_per_service()
    for r in rows:
        r.status = "operational"
    assert overall_status(rows) == "operational"


def test_overall_with_critical_down_is_outage():
    rows = latest_per_service()
    for r in rows:
        r.status = "operational"
    next(r for r in rows if r.name == "Public Site").status = "down"
    assert overall_status(rows) == "outage"


def test_overall_with_non_critical_down_is_partial():
    rows = latest_per_service()
    for r in rows:
        r.status = "operational"
    next(r for r in rows if r.name == "Bot Worker").status = "down"
    assert overall_status(rows) == "partial_outage"


def test_overall_with_only_degraded_is_degraded():
    rows = latest_per_service()
    for r in rows:
        r.status = "operational"
    next(r for r in rows if r.name == "Bot").status = "degraded"
    assert overall_status(rows) == "degraded"


def test_roll_up_creates_daily_uptime_row():
    _insert_probe("Public Site", "operational")
    roll_up_after_probe()
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM daily_uptime WHERE service_name='Public Site'").fetchall()
    assert len(rows) == 1
    assert rows[0]["uptime_pct"] == 100.0


def test_roll_up_records_failures():
    _insert_probe("Public Site", "operational")
    _insert_probe("Public Site", "down")
    _insert_probe("Public Site", "operational")
    roll_up_after_probe()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM daily_uptime WHERE service_name='Public Site'").fetchone()
    assert row["total_checks"] == 3
    assert row["failed_checks"] == 1
    assert abs(row["uptime_pct"] - 66.667) < 0.1


def test_incident_opens_on_down_and_closes_on_recovery():
    _insert_probe("Bot", "down", source="external")
    roll_up_after_probe()
    with db.connect() as conn:
        open_inc = conn.execute("SELECT * FROM incidents WHERE service_name='Bot' AND resolved=0").fetchone()
    assert open_inc is not None

    _insert_probe("Bot", "operational", source="external")
    roll_up_after_probe()
    with db.connect() as conn:
        resolved_inc = conn.execute("SELECT * FROM incidents WHERE service_name='Bot' AND resolved=1").fetchone()
    assert resolved_inc is not None
    assert resolved_inc["duration_min"] >= 1


def test_sla_summary_below_target():
    _insert_probe("Public Site", "down")
    roll_up_after_probe()
    sla = sla_summary(target_pct=99.9)
    assert sla["below_target"] is True
