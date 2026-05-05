from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from status_service.main import app

SECRET = "test-secret-deadbeef"


def _sign(body: bytes, ts: int | None = None) -> dict[str, str]:
    if ts is None:
        ts = int(time.time())
    msg = f"{ts}.".encode("utf-8") + body
    sig = hmac.new(SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {"X-Status-Timestamp": str(ts), "X-Status-Signature": sig}


def test_announce_rejects_unsigned_request():
    with TestClient(app) as client:
        r = client.post("/admin/announce", json={"type": "incident", "severity": "critical", "title": "x", "body": "y"})
    assert r.status_code == 401


def test_announce_accepts_signed_request():
    body = json.dumps({"type": "incident", "severity": "critical", "title": "Database slow", "body": "Investigating."}).encode("utf-8")
    headers = _sign(body)
    with TestClient(app) as client:
        r = client.post("/admin/announce", content=body, headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_announce_rejects_replay_outside_window():
    body = json.dumps({"type": "incident", "severity": "info", "title": "x", "body": "y"}).encode("utf-8")
    old_ts = int(time.time()) - 600  # 10 min ago, outside 5-min window
    headers = _sign(body, ts=old_ts)
    with TestClient(app) as client:
        r = client.post("/admin/announce", content=body, headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 401


def test_announce_update_lifecycle():
    body = json.dumps({"type": "incident", "severity": "critical", "title": "x", "body": "y"}).encode("utf-8")
    headers = _sign(body)
    with TestClient(app) as client:
        r = client.post("/admin/announce", content=body, headers={**headers, "Content-Type": "application/json"})
        ann_id = r.json()["id"]

        upd_body = json.dumps({"status": "monitoring", "body": "Mitigations applied."}).encode("utf-8")
        upd_headers = _sign(upd_body)
        r = client.post(f"/admin/announce/{ann_id}/update", content=upd_body, headers={**upd_headers, "Content-Type": "application/json"})
        assert r.status_code == 200

        resolve_body = b""
        resolve_headers = _sign(resolve_body)
        r = client.post(f"/admin/announce/{ann_id}/resolve", content=resolve_body, headers=resolve_headers)
        assert r.status_code == 200


def test_admin_disabled_when_no_secret(monkeypatch):
    monkeypatch.setenv("ADMIN_HMAC_SECRET", "")
    from status_service.config import reset_settings
    reset_settings()
    body = b"{}"
    headers = _sign(body)
    with TestClient(app) as client:
        r = client.post("/admin/announce", content=body, headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 503
