from __future__ import annotations

from fastapi.testclient import TestClient

from status_service import db
from status_service.main import app


def test_feed_is_valid_rss_with_items():
    # an announcement + a resolved incident with a cause
    with db.connect() as conn:
        conn.execute("INSERT INTO announcements(type,severity,title,body) VALUES "
                     "('incident','warning','API latency','Investigating elevated latency.')")
        conn.execute("INSERT INTO incidents(service_name,started_at,ended_at,duration_min,resolved,cause,cause_at) "
                     "VALUES ('Gateway','2026-06-01T03:00:00.000Z','2026-06-01T03:40:00.000Z',40,1,"
                     "'Bad config push; rolled back.','2026-06-01T04:00:00.000Z')")
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
        conn.execute("INSERT INTO incidents(service_name,started_at,resolved) VALUES "
                     "('Cache','2026-06-02T00:00:00.000Z',0)")  # ongoing, no cause
    with TestClient(app) as client:
        body = client.get("/feed.xml").text
    assert "Cache" not in body
