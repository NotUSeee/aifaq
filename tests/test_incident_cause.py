from __future__ import annotations

import hashlib
import hmac
import json
import time

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


def _make_incident(service_name: str = "Gateway") -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO incidents(service_name, started_at, resolved) VALUES (?,?,0)",
            (service_name, "2026-06-01T03:00:00.000Z"),
        )
        return int(cur.lastrowid)


def test_cause_rejects_unsigned_request():
    inc = _make_incident()
    with TestClient(app) as client:
        r = client.post(f"/admin/incident/{inc}/cause", json={"cause": "x"})
    assert r.status_code == 401


def test_cause_404_on_missing_incident():
    body = json.dumps({"cause": "Root cause"}).encode("utf-8")
    headers = _sign(body)
    with TestClient(app) as client:
        r = client.post(
            "/admin/incident/999999/cause",
            content=body,
            headers={**headers, "Content-Type": "application/json"},
        )
    assert r.status_code == 404


def test_cause_persists_and_is_idempotent():
    inc = _make_incident()
    body = json.dumps(
        {"cause": "Upstream DNS resolver outage; failed over to the secondary."}
    ).encode("utf-8")
    headers = _sign(body)
    with TestClient(app) as client:
        r = client.post(
            f"/admin/incident/{inc}/cause",
            content=body,
            headers={**headers, "Content-Type": "application/json"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # Overwrite (idempotent) refreshes cause + cause_at.
        body2 = json.dumps({"cause": "Corrected: bad config push, rolled back."}).encode("utf-8")
        r2 = client.post(
            f"/admin/incident/{inc}/cause",
            content=body2,
            headers={**_sign(body2), "Content-Type": "application/json"},
        )
        assert r2.status_code == 200

    with db.connect() as conn:
        row = conn.execute(
            "SELECT cause, cause_at FROM incidents WHERE id=?", (inc,)
        ).fetchone()
    assert row["cause"].startswith("Corrected:")
    assert row["cause_at"]


def test_cause_invalid_payload_returns_422_not_500():
    inc = _make_incident()
    body = b'{"cause": ""}'  # violates min_length=1
    headers = _sign(body)
    with TestClient(app) as client:
        r = client.post(
            f"/admin/incident/{inc}/cause",
            content=body,
            headers={**headers, "Content-Type": "application/json"},
        )
    assert r.status_code == 422


def test_cause_renders_publicly_after_set():
    inc = _make_incident("Gateway")
    body = json.dumps({"cause": "Datacenter network blip; auto-recovered."}).encode("utf-8")
    with TestClient(app) as client:
        client.post(
            f"/admin/incident/{inc}/cause",
            content=body,
            headers={**_sign(body), "Content-Type": "application/json"},
        )
        page = client.get("/")
    assert page.status_code == 200
    assert "Why this happened" in page.text
    assert "Datacenter network blip" in page.text


def test_index_renders_rebranded():
    """The page renders and carries the new brand wiring, not the old."""
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "yourbot-logo.png" in html          # new logo
    assert "status.css?v=6" in html            # cache-buster bumped
    assert "⚔" not in html                # no medieval ⚔ glyph anywhere
    assert "All Systems Operational" in html or "Checking status" in html  # banner
    assert "component-row" in html             # grouped components present
    assert "YourBot Official Site" in html     # new header
    assert "EmberStream Studio" in html        # homepage-matching footer
    assert "nav-hamburger" not in html         # old nav removed
