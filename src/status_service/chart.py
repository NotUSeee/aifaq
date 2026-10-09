"""Geometry for the website response-time chart.

Built on the server so the chart is part of the page itself: no chart
library, nothing to download, and it still shows with scripts off. The
script only adds the crosshair readout and local-time axis labels.

Every coordinate is a percentage of the plot box (x: 0 left to 100 right,
y: 0 top to 100 bottom). The marks are an SVG stretched over that box while
labels and dots are ordinary HTML placed by percentage, so text stays a
readable size on a phone instead of shrinking with the drawing.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from .aggregator import _parse_iso, _to_iso, response_time_series

SERVICE = "Public Site"


def _nice_max(value: float) -> int:
    """Round the top of the y-axis up to a clean number of milliseconds."""
    if value <= 0:
        return 100
    for step in (100, 200, 300, 400, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000):
        if value <= step:
            return step
    return int(math.ceil(value / 5000.0) * 5000)


def build_response_chart(hours: int = 24) -> dict | None:
    data = response_time_series(hours=hours)
    points = data["series"].get(SERVICE) or []
    if len(points) < 2:
        return None

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    span = (end - start).total_seconds()
    y_max = _nice_max(max(p["p95"] for p in points))
    bucket = data["bucket_seconds"]

    def x_of(ts: datetime) -> float:
        frac = min(1.0, max(0.0, (ts - start).total_seconds() / span))
        return round(frac * 100, 2)

    def y_of(ms: float) -> float:
        frac = min(1.0, max(0.0, ms / y_max))
        return round((1 - frac) * 100, 2)

    # Split into runs so a stretch with no data is a gap, not a line drawn
    # straight across it.
    runs: list[list[dict]] = []
    prev: datetime | None = None
    for p in points:
        ts = min(end, _parse_iso(p["t"]) + timedelta(seconds=bucket / 2))
        item = {"ts": ts, "x": x_of(ts), "y50": y_of(p["p50"]), "y95": y_of(p["p95"]),
                "p50": p["p50"], "p95": p["p95"]}
        if prev is None or (ts - prev).total_seconds() > bucket * 2.5:
            runs.append([])
        runs[-1].append(item)
        prev = ts

    lines: list[str] = []
    bands: list[str] = []
    for run in runs:
        if len(run) == 1:
            continue
        lines.append(" ".join(f"{p['x']},{p['y50']}" for p in run))
        upper = [f"{p['x']},{p['y95']}" for p in run]
        lower = [f"{p['x']},{p['y50']}" for p in reversed(run)]
        bands.append(" ".join(upper + lower))

    y_ticks = [{"y": y_of(v), "label": f"{int(v):,}"} for v in (y_max, y_max / 2, 0)]

    x_ticks = []
    step_hours = 6 if hours >= 18 else max(1, hours // 4)
    tick = (start + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    while tick.hour % step_hours:
        tick += timedelta(hours=1)
    while tick < end:
        x = x_of(tick)
        if 3 <= x <= 97:  # a label hugging either edge would be clipped
            x_ticks.append({"x": x, "ts": _to_iso(tick), "label": tick.strftime("%H:%M") + " UTC"})
        tick += timedelta(hours=step_hours)

    flat = [p for run in runs for p in run]
    last = flat[-1]
    p50s = sorted(p["p50"] for p in flat)
    return {
        "lines": lines, "bands": bands,
        "y_ticks": y_ticks, "x_ticks": x_ticks,
        "last": {"x": last["x"], "y": last["y50"], "label": f"{last['p50']} ms"},
        "typical_ms": p50s[len(p50s) // 2],
        "hours": hours,
        "points_json": json.dumps(
            [{"x": p["x"], "y": p["y50"], "t": _to_iso(p["ts"]), "p50": p["p50"], "p95": p["p95"]} for p in flat],
            separators=(",", ":"),
        ),
    }
