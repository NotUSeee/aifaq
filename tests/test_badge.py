from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from status_service import db
from status_service.main import app


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _seed_status(name: str, status: str):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO probe_results(service_name,status,response_ms,source,checked_at) "
            "VALUES (?,?,?,?,?)",
            (name, status, 50, "external", _iso(datetime.now(timezone.utc))),
        )


def test_badge_returns_svg():
    _seed_status("Public Site", "operational")
    with TestClient(app) as client:
        r = client.get("/badge.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers.get("cache-control", "").startswith("public")
    assert "<svg" in r.text


def test_badge_color_changes_with_status():
    _seed_status("Public Site", "down")
    with TestClient(app) as client:
        r = client.get("/badge.svg")
    assert "#e05a5a" in r.text  # red

    _seed_status("Public Site", "operational")
    with TestClient(app) as client:
        r = client.get("/badge.svg")
    assert "#6bcb8b" in r.text  # green
