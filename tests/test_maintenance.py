"""Scheduled-maintenance windows: HMAC API validation, auto-expiry,
public-page "Scheduled" section, and feed rendering."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from status_service import db
from status_service.main import app

SECRET = "test-secret-deadbeef"


def _sign(body: bytes, ts: int | None = None) -> dict[str, str]:
    if ts is None:
        ts = int(time.time())
    msg = f"{ts}.".encode("utf-8") + body
    sig = hmac.new(SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {"X-Status-Timestamp": str(ts), "X-Status-Signature": sig}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _announce(client: TestClient, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        "/admin/announce",
        content=body,
        headers={**_sign(body), "Content-Type": "application/json"},
    )


def test_announce_with_window_persists_normalized():
    starts = _iso(datetime.now(timezone.utc) + timedelta(days=2))
    ends = _iso(datetime.now(timezone.utc) + timedelta(days=2, hours=1))
    with TestClient(app) as client:
        r = _announce(client, {
            "type": "maintenance", "severity": "info",
            "title": "DB upgrade", "body": "Postgres major version bump.",
            "starts_at": starts, "ends_at": ends,
        })
    assert r.status_code == 200
    with db.connect() as conn:
        row = conn.execute("SELECT starts_at, ends_at FROM announcements WHERE id=?",
                           (r.json()["id"],)).fetchone()
    assert row["starts_at"] == starts
    assert row["ends_at"] == ends


def test_announce_window_offset_normalized_to_utc_z():
    with TestClient(app) as client:
        r = _announce(client, {
            "type": "maintenance", "severity": "info", "title": "x", "body": "y",
            "starts_at": "2027-01-01T05:00:00+02:00",
        })
    assert r.status_code == 200
    with db.connect() as conn:
        row = conn.execute("SELECT starts_at FROM announcements WHERE id=?",
                           (r.json()["id"],)).fetchone()
    assert row["starts_at"] == "2027-01-01T03:00:00.000Z"


@pytest.mark.parametrize("payload", [
    # Window on an incident is meaningless.
    {"type": "incident", "severity": "info", "title": "x", "body": "y",
     "starts_at": "2027-01-01T00:00:00Z"},
    # End before start.
    {"type": "maintenance", "severity": "info", "title": "x", "body": "y",
     "starts_at": "2027-01-02T00:00:00Z", "ends_at": "2027-01-01T00:00:00Z"},
    # Garbage timestamp.
    {"type": "maintenance", "severity": "info", "title": "x", "body": "y",
     "starts_at": "not-a-date"},
])
def test_announce_window_validation_422(payload):
    with TestClient(app) as client:
        r = _announce(client, payload)
    assert r.status_code == 422


def test_expire_ended_maintenance_resolves_past_windows_only():
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO announcements(type,severity,title,body,starts_at,ends_at) VALUES "
            "('maintenance','info','past','x',?,?)",
            (_iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=1))))
        conn.execute(
            "INSERT INTO announcements(type,severity,title,body,starts_at,ends_at) VALUES "
            "('maintenance','info','future','x',?,?)",
            (_iso(now + timedelta(hours=1)), _iso(now + timedelta(hours=2))))
        conn.execute(
            "INSERT INTO announcements(type,severity,title,body) VALUES "
            "('maintenance','info','unscheduled','x')")

    assert db.expire_ended_maintenance() == 1

    with db.connect() as conn:
        rows = {r["title"]: r["resolved_at"] for r in conn.execute(
            "SELECT title, resolved_at FROM announcements").fetchall()}
    assert rows["past"] is not None
    assert rows["future"] is None
    assert rows["unscheduled"] is None


def test_public_page_splits_scheduled_from_active():
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO announcements(type,severity,title,body,starts_at,ends_at) VALUES "
            "('maintenance','info','Planned relocation','Moving racks.',?,?)",
            (_iso(now + timedelta(days=1)), _iso(now + timedelta(days=1, hours=2))))
        conn.execute(
            "INSERT INTO announcements(type,severity,title,body) VALUES "
            "('incident','critical','Live problem','Right now.')")
    with TestClient(app) as client:
        html = client.get("/").text
    assert "Planned relocation" in html
    assert ">Scheduled<" in html                 # scheduled tag, not a live banner
    assert "announcement-scheduled" in html
    assert "Live problem" in html
    # Permalink anchors present for both.
    assert 'id="announcement-' in html


def test_feed_marks_scheduled_maintenance_and_links_anchor():
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO announcements(type,severity,title,body,starts_at,ends_at) VALUES "
            "('maintenance','info','Planned relocation','Moving racks.',?,?)",
            (_iso(now + timedelta(days=1)), _iso(now + timedelta(days=1, hours=2))))
    with TestClient(app) as client:
        xml = client.get("/feed.xml").text
    assert "Scheduled maintenance: Planned relocation" in xml
    assert "/#announcement-" in xml
    assert "Window:" in xml
