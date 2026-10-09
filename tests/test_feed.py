from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from status_service import db
from status_service.main import app


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_feed_is_valid_rss_with_items():
    # an announcement + a resolved incident with a cause. Dates are relative:
    # fixed ones silently age out of the feed's 90-day window.
    start = datetime.now(timezone.utc) - timedelta(days=3)
    with db.connect() as conn:
        conn.execute("INSERT INTO announcements(type,severity,title,body) VALUES "
                     "('incident','warning','API latency','Investigating elevated latency.')")
        conn.execute("INSERT INTO incidents(service_name,started_at,ended_at,duration_min,resolved,cause,cause_at) "
                     "VALUES ('Gateway',?,?,40,1,'Bad config push; rolled back.',?)",
                     (_iso(start), _iso(start + timedelta(minutes=40)), _iso(start + timedelta(hours=1))))
    with TestClient(app) as client:
        r = client.get("/feed.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/rss+xml")
    body = r.text
    assert "<rss" in body and "</rss>" in body
    assert "<title>YourBot Status</title>" in body
    assert "API latency" in body          # announcement
    assert "Bad config push" in body      # incident cause
    # XML-escaped, well-formed
    import xml.dom.minidom as md
    md.parseString(r.content)  # raises if malformed


def test_feed_skips_unexplained_ongoing_incidents():
    with db.connect() as conn:
        conn.execute("INSERT INTO incidents(service_name,started_at,resolved) VALUES ('Cache',?,0)",
                     (_iso(datetime.now(timezone.utc) - timedelta(hours=2)),))  # ongoing, no cause
    with TestClient(app) as client:
        body = client.get("/feed.xml").text
    assert "Cache" not in body
