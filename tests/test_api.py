from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from status_service import db
from status_service.main import app


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _seed(name: str, status: str, ms: int = 50):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO probe_results(service_name,status,response_ms,http_status,source,checked_at) "
            "VALUES (?,?,?,?,?,?)",
            (name, status, ms, 200, "external", _iso(datetime.now(timezone.utc))),
        )


def test_api_contract_matches_platform_schema():
    """The new /api response must include the same top-level fields the
    platform's /status/api exposes (current, overall, service_order)
    so the rendering layer is interchangeable."""
    _seed("Public Site", "operational")
    with TestClient(app) as client:
        r = client.get("/api")
    assert r.status_code == 200
    payload = r.json()
    assert "current" in payload
    assert "overall" in payload
    assert "service_order" in payload
    assert "meta" in payload
    assert isinstance(payload["current"], list)
    assert isinstance(payload["service_order"], list)


def test_api_includes_meta_fields():
    _seed("Public Site", "operational")
    with TestClient(app) as client:
        r = client.get("/api")
    meta = r.json()["meta"]
    assert "staleness_seconds" in meta
    assert "probe_interval_seconds" in meta
    assert "sla" in meta
    assert "now" in meta


def test_api_no_cache_headers():
    with TestClient(app) as client:
        r = client.get("/api")
    assert "no-store" in r.headers.get("cache-control", "")


def test_health_endpoint():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_graph_endpoint():
    _seed("Public Site", "operational")
    with TestClient(app) as client:
        r = client.get("/api/graph?hours=6")
    assert r.status_code == 200
    assert "series" in r.json()


def test_timeline_endpoint():
    _seed("Public Site", "operational")
    with TestClient(app) as client:
        r = client.get("/api/timeline?days=90")
    assert r.status_code == 200
    body = r.json()
    assert "days" in body
    assert "series" in body


def test_shards_endpoint_empty_state():
    with TestClient(app) as client:
        r = client.get("/api/shards")
    assert r.status_code == 200
    body = r.json()
    assert body["clusters"] == []
    assert body["totals"]["shards"] == 0
