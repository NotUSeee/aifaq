from __future__ import annotations

from fastapi.testclient import TestClient

from status_service import db
from status_service.main import app

PASSWORD = "test-secret-deadbeef"  # conftest sets ADMIN_HMAC_SECRET to this


def _make_incident(service_name: str = "Gateway") -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO incidents(service_name, started_at, resolved) VALUES (?,?,1)",
            (service_name, "2026-06-01T03:00:00.000Z"),
        )
        return int(cur.lastrowid)


def test_admin_home_shows_login_when_unauthed():
    with TestClient(app) as client:
        r = client.get("/admin")
    assert r.status_code == 200
    assert 'name="password"' in r.text
    assert "Save reason" not in r.text  # panel not shown


def test_admin_login_wrong_password_401():
    with TestClient(app) as client:
        r = client.post("/admin/login", data={"password": "nope"}, follow_redirects=False)
    assert r.status_code == 401
    assert "Incorrect password" in r.text


def test_admin_login_sets_cookie_and_opens_panel():
    with TestClient(app) as client:
        r = client.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
        assert r.status_code == 303
        assert "yb_admin" in r.headers.get("set-cookie", "")
        panel = client.get("/admin")  # cookie jar carries the session
        assert panel.status_code == 200
        assert "Status admin" in panel.text
        assert "Post a new announcement" in panel.text


def test_cause_form_requires_auth():
    inc = _make_incident()
    with TestClient(app) as client:
        r = client.post(f"/admin/incident/{inc}/cause-form", data={"cause": "x"}, follow_redirects=False)
    assert r.status_code == 401


def test_cause_form_sets_and_clears_reason_when_authed():
    inc = _make_incident()
    with TestClient(app) as client:
        client.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
        # set
        r = client.post(f"/admin/incident/{inc}/cause-form",
                        data={"cause": "Bad deploy; rolled back."}, follow_redirects=False)
        assert r.status_code == 303
        with db.connect() as conn:
            row = conn.execute("SELECT cause, cause_at FROM incidents WHERE id=?", (inc,)).fetchone()
        assert row["cause"] == "Bad deploy; rolled back."
        assert row["cause_at"]
        # public page renders it
        assert "Bad deploy; rolled back." in client.get("/").text
        # clear (empty box removes it)
        client.post(f"/admin/incident/{inc}/cause-form", data={"cause": "   "}, follow_redirects=False)
        with db.connect() as conn:
            row = conn.execute("SELECT cause, cause_at FROM incidents WHERE id=?", (inc,)).fetchone()
        assert row["cause"] is None
        assert row["cause_at"] is None


def test_announce_form_creates_and_resolves():
    with TestClient(app) as client:
        client.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
        client.post("/admin/announce-form", data={
            "type": "incident", "severity": "warning",
            "title": "API latency", "body": "Investigating elevated latency.",
        }, follow_redirects=False)
        with db.connect() as conn:
            row = conn.execute("SELECT id, resolved_at FROM announcements WHERE title='API latency'").fetchone()
        assert row is not None and row["resolved_at"] is None
        ann_id = row["id"]
        # shows on the public page
        assert "API latency" in client.get("/").text
        # resolve
        client.post(f"/admin/announce/{ann_id}/resolve-form", follow_redirects=False)
        with db.connect() as conn:
            row = conn.execute("SELECT resolved_at FROM announcements WHERE id=?", (ann_id,)).fetchone()
        assert row["resolved_at"] is not None


def test_admin_disabled_without_secret(monkeypatch):
    monkeypatch.setenv("ADMIN_HMAC_SECRET", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    from status_service.config import reset_settings
    reset_settings()
    with TestClient(app) as client:
        r = client.get("/admin")
        assert r.status_code == 200
        assert "disabled" in r.text.lower()
        # login refuses when disabled
        assert client.post("/admin/login", data={"password": "x"}, follow_redirects=False).status_code == 503
