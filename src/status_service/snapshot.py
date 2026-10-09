"""Everything the public page and /api show, computed once and shared.

The page, the live-refresh fragment and the JSON API all describe the same
moment, so they are built from one snapshot. It is cached for a few seconds:
checks only land once a minute, and during an incident (exactly when many
people open the page) recomputing it per request would be wasted work on a
small box.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta, timezone

from . import aggregator as agg
from . import ingest
from .config import get_settings

logger = logging.getLogger("status_service.snapshot")

HEADLINES = {
    "operational": "All systems operational",
    "degraded": "Degraded performance",
    "partial_outage": "Partial outage",
    "outage": "Major outage",
    "unknown": "Status checks paused",
    # Nothing is failing, but not everything is being measured.
    "limited": "Some systems are not reporting",
}

STATUS_LABEL = {
    "operational": "Operational",
    "degraded": "Degraded",
    "partial": "Partial outage",
    "down": "Outage",
    "unknown": "No data",
}
CHECK_LABEL = {**STATUS_LABEL, "down": "Down"}

TIMELINE_DAYS = 90
# A day with more than this share of failed checks is drawn as major.
_MAJOR_DAY_PCT = 99.0

_cache: dict[str, tuple[float, dict]] = {}


def clear_cache() -> None:
    _cache.clear()


def get_snapshot() -> dict:
    settings = get_settings()
    ttl = max(0.0, float(settings.api_cache_seconds))
    key = settings.db_path
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and ttl > 0 and now - hit[0] < ttl:
        return hit[1]
    snap = build_snapshot()
    if ttl > 0:
        _cache[key] = (now, snap)
    return snap


def fmt_pct(window: dict | None) -> str:
    """Uptime as text. Never rounds UP: 99.996% with a failed check must not
    read "100%", so the figure is floored to two decimals."""
    if not window or window.get("pct") is None:
        return "–"
    if not window.get("failed"):
        return "100%"
    floored = math.floor(float(window["pct"]) * 100) / 100
    return f"{min(floored, 99.99):.2f}%"


def fmt_pct_value(pct: float | None, exact_100: bool = False) -> str:
    if pct is None:
        return "–"
    if exact_100 or pct >= 100.0:
        return "100%"
    floored = math.floor(float(pct) * 100) / 100
    return f"{min(floored, 99.99):.2f}%"


def downtime_text(failed: int, interval_seconds: int) -> str:
    minutes = failed * interval_seconds / 60.0
    if minutes < 1:
        return "under a minute of downtime"
    return f"about {agg.format_duration(int(round(minutes)))} of downtime"


def _day_label(day_iso: str) -> str:
    d = date.fromisoformat(day_iso)
    return f"{d.strftime('%b')} {d.day}"


def _timeline_cells(entries: list[dict], today: date, interval_seconds: int) -> tuple[list[dict], dict]:
    """TIMELINE_DAYS cells, oldest first, for one component."""
    by_day = {e["day"]: e for e in entries}
    cells: list[dict] = []
    measured = with_downtime = 0
    for back in range(TIMELINE_DAYS - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        e = by_day.get(day)
        label = _day_label(day)
        if not e or not e.get("total_checks"):
            cells.append({"day": day, "cls": "none", "tip": f"{label}: not measured"})
            continue
        measured += 1
        failed = int(e.get("failed_checks") or 0)
        if failed <= 0:
            cells.append({"day": day, "cls": "ok", "tip": f"{label}: no downtime"})
            continue
        with_downtime += 1
        cls = "major" if float(e["uptime_pct"]) < _MAJOR_DAY_PCT else "minor"
        pct_text = fmt_pct({"pct": e["uptime_pct"], "failed": failed})
        cells.append({
            "day": day, "cls": cls,
            "tip": f"{label}: {downtime_text(failed, interval_seconds)}, {pct_text} uptime",
        })
    return cells, {"measured": measured, "with_downtime": with_downtime}


SHARD_LABEL = {"operational": "Online", "degraded": "Degraded", "down": "Down"}


def _shard_health(shards: dict) -> dict:
    """The shared bot's connections to Discord, for the "Right now" figures.

    A shard whose state could not be read is "not measured", never offline:
    with nothing measured there is no count to show at all. Each shard is
    listed once there is more than one of them."""
    totals = shards["totals"]
    total = int(totals["shards"])
    measured = total - int(totals["unknown"])
    notes = [f"{totals[key]} {word}" for key, word in
             (("degraded", "degraded"), ("down", "down"), ("unknown", "not measured"))
             if totals[key] and measured]
    rows = []
    if total > 1:
        for cluster in shards["clusters"]:
            for s in cluster["shards"]:
                status = s["status"] if s["status"] in SHARD_LABEL else "unknown"
                rows.append({
                    "shard_id": s["shard_id"],
                    "status": status,
                    "status_label": SHARD_LABEL.get(status, "No data"),
                    "latency_ms": s["latency_ms"] if status in ("operational", "degraded") else None,
                    "servers": s["guild_count"],
                })
    return {
        "total": total,
        "online": int(totals["online"]) if measured else None,
        "note": ", ".join(notes) if notes else ("not measured right now" if total and not measured else None),
        "rows": rows,
    }


def _banner_detail(overall: str, own: list[dict], currents: list, monitor: dict, discord: dict | None) -> str:
    if overall == "unknown":
        if monitor.get("online") is False:
            return ("Our monitor has lost its internet connection, so nothing can be checked right now. "
                    "That is a problem with the monitor, not a sign that YourBot is down.")
        return "Waiting for the first results."
    if overall == "limited":
        silent = [c["name"] for c in own if c.get("core") and c["status"] == "unknown"]
        return ("No data right now from: " + ", ".join(silent) + ". "
                "Everything we can measure is working. This usually means a checker has stopped, not the service.")
    affected = [c["name"] for c in own if c["status"] in ("down", "partial", "degraded")]
    parts: list[str] = []
    if affected:
        parts.append("Affected: " + ", ".join(affected) + ".")
    site_down = any(c.name == "Public Site" and c.status == "down" for c in currents)
    if site_down:
        still_reporting = any(c["status"] != "unknown" for c in own if c["key"] != "website")
        if still_reporting:
            parts.append("The website cannot be reached. The other systems are still reporting to us directly.")
        else:
            parts.append("The website cannot be reached, so the other systems cannot be checked until it is back.")
    if not parts and discord and discord["status"] in ("degraded", "down"):
        parts.append("Discord is reporting problems of its own, which can slow or stop any bot.")
    return " ".join(parts)


def _release_notice() -> dict | None:
    """A release going out right now, as the page shows it. Only that it is
    happening and since when: the pipeline's own name for the build means
    nothing to a visitor, and it is not the version in the patch notes."""
    try:
        release = ingest.current_release()
    except Exception:
        # The page must still render. Without the notice it says less, not something false.
        logger.exception("could not read the release in progress")
        return None
    return {"started_at": release["started_at"]} if release else None


def _live_test_note(components: list[dict], settings) -> dict | None:
    """How often the live test runs, when it is one of the checks shown."""
    if not any(chk["name"] == "Bot Response" for comp in components for chk in comp["checks"]):
        return None
    interval = max(30, int(settings.live_test_interval_seconds))
    return {"interval_text": "Every minute" if interval < 90 else f"Every {round(interval / 60)} minutes"}


def build_snapshot() -> dict:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    interval = settings.probe_interval_seconds

    currents = agg.latest_per_service()
    windows = agg.uptime_windows()
    components = agg.build_components(currents, windows)
    timeline = agg.daily_uptime_series(days=TIMELINE_DAYS)
    monitor = agg.monitor_state()
    overall = agg.overall_status(currents)

    for comp in components:
        comp["status_label"] = STATUS_LABEL.get(comp["status"], "No data")
        comp["uptime_text"] = {k: fmt_pct(comp["uptime"].get(k)) for k, _ in agg.UPTIME_WINDOWS}
        cells, stats = _timeline_cells(timeline["groups"].get(comp["key"], []), now.date(), interval)
        comp["cells"] = cells
        comp["timeline_summary"] = (
            f"Last {TIMELINE_DAYS} days: {stats['measured']} measured, "
            f"{stats['with_downtime']} with downtime"
        )
        # Each check gets its own 90-day bar. Diagnostics have none (they are
        # never part of an uptime figure), and where a component rests on a
        # single counted check the bar above is already that check's.
        counted = [chk for chk in comp["checks"] if not chk["advisory"]]
        for chk in comp["checks"]:
            chk["status_label"] = CHECK_LABEL.get(chk["status"], "No data")
            chk["uptime_text"] = {k: fmt_pct(chk["uptime"].get(k)) for k, _ in agg.UPTIME_WINDOWS}
            chk["cells"] = None
            if comp["third_party"] or chk["advisory"] or len(counted) < 2:
                continue
            chk["cells"], chk_stats = _timeline_cells(timeline["series"].get(chk["name"], []), now.date(), interval)
            chk["timeline_summary"] = (
                f"Last {TIMELINE_DAYS} days: {chk_stats['measured']} measured, "
                f"{chk_stats['with_downtime']} with downtime"
            )

    # Where the website is checked from, and whether the platform is
    # sending its own report to us directly. Shown so the page describes
    # how it really measures right now, not how it could.
    places = ingest.places()
    place_votes = [p for p in places if p["fresh"]]
    push_age = ingest.platform_report_age()
    for comp in components:
        for chk in comp["checks"]:
            if chk["name"] == "Public Site" and len(places) > 1:
                chk["how"] = f"Checked from {len(places)} places"
                reached = sum(1 for p in place_votes if p["status"] in ("operational", "degraded"))
                if place_votes:
                    chk["places_text"] = (f"Reached from {reached} of {len(place_votes)} places just now: "
                                          + ", ".join(p["label"] for p in places) + ".")

    own = [c for c in components if not c["third_party"]]
    third = [c for c in components if c["third_party"]]
    discord = third[0] if third else None
    discord_note = None
    if discord:
        discord_note = next((chk["note"] for chk in discord["checks"]
                             if chk["note"] and chk["status"] in ("degraded", "down")), None)

    headline = agg.headline_uptime(components)
    core_failed = {
        k: any((c["uptime"].get(k) or {}).get("failed") for c in components if c.get("core"))
        for k, _ in agg.UPTIME_WINDOWS
    }
    headline_text = {
        k: fmt_pct_value(headline[k], exact_100=(headline[k] is not None and not core_failed[k]))
        for k, _ in agg.UPTIME_WINDOWS
    }

    since = agg.data_since()
    days_of_data = None
    if since:
        try:
            days_of_data = (now.date() - date.fromisoformat(since)).days + 1
        except ValueError:
            days_of_data = None

    newest = agg.newest_probe_at()
    shards = agg.shard_summary()
    site = next((c for c in currents if c.name == "Public Site"), None)

    return {
        "now": agg._to_iso(now),
        "overall": overall,
        "headline": HEADLINES.get(overall, HEADLINES["unknown"]),
        "detail": _banner_detail(overall, own, currents, monitor, discord),
        "checked_at": agg._to_iso(newest) if newest else None,
        "staleness_seconds": (now - newest).total_seconds() if newest else None,
        "probe_interval_seconds": interval,
        "interval_text": "every minute" if interval < 90 else f"every {round(interval / 60)} minutes",
        "monitor": monitor,
        "places": places,
        "platform_direct": push_age is not None and push_age <= 600,
        "release": _release_notice(),
        # Said on the page only where it is true of this deployment.
        "release_notices": len(settings.ingest_release_secret or "") >= 32,
        "live_test": _live_test_note(components, settings),
        "currents": currents,
        "components": own,
        "third_party": third,
        "discord_note": discord_note,
        "uptime": headline,
        "uptime_text": headline_text,
        "windows": [{"key": k, "label": agg.WINDOW_LABEL[k]} for k, _ in agg.UPTIME_WINDOWS if k != "90d"],
        "data_since": since,
        "days_of_data": days_of_data,
        "timeline_days": TIMELINE_DAYS,
        "numbers": {
            "servers": shards["totals"]["guilds"] or None,
            "shards": _shard_health(shards),
            "bot_latency_ms": shards["latency_ms"],
            "site_response_ms": site.response_ms if site and site.status in ("operational", "degraded") else None,
        },
    }
