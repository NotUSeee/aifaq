from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from status_service import admin_auth as aa
from status_service import db
from status_service.config import reset_settings
from status_service.main import app

SECRET = "test-secret-deadbeef"  # conftest sets ADMIN_HMAC_SECRET to this
BOOT = "boot-token-xyz"


def _code(secret: str, offset: int = 0) -> str:
    return aa.totp_at(secret, int(time.time()) // aa.TOTP_PERIOD + offset)


def _enable_bootstrap(monkeypatch):
    monkeypatch.setenv("ADMIN_BOOTSTRAP_TOKEN", BOOT)
    reset_settings()


def _bootstrap_owner(client, username="owner1", password="supersecret123"):
    secret = aa.new_totp_secret()
    field = aa.sign_secret_field(SECRET, secret, BOOT)
    # Confirm setup with the PREVIOUS window's code so an immediate login with
    # the CURRENT code isn't rejected by the replay guard (real users rarely
    # log in within the same 30s; the test would otherwise be flaky).
    r = client.post("/admin/setup", data={
        "token": BOOT, "username": username, "password": password, "password2": password,
        "secret_field": field, "code": _code(secret, -1),
    }, follow_redirects=False)
    return r, secret


def _staff_setup(client, token, password, secret):
    field = aa.sign_secret_field(SECRET, secret, token)
    return client.post("/admin/setup", data={
        "token": token, "password": password, "password2": password,
        "secret_field": field, "code": _code(secret, -1)}, follow_redirects=False)


# ── crypto units ─────────────────────────────────────────────────────────
def test_password_hash_roundtrip():
    h, salt = aa.hash_password("hunter2hunter2")
    assert aa.verify_password("hunter2hunter2", salt, h)
    assert not aa.verify_password("wrong", salt, h)


def test_totp_and_replay():
    s = aa.new_totp_secret()
    step = int(time.time()) // aa.TOTP_PERIOD
    ok, used = aa.verify_totp(s, aa.totp_at(s, step))
    assert ok and used == step
    # the same step can't be replayed once recorded
    ok2, _ = aa.verify_totp(s, aa.totp_at(s, step), last_step=used)
    assert not ok2


# ── flows ────────────────────────────────────────────────────────────────
def test_setup_bootstrap_creates_owner_then_login(monkeypatch):
    _enable_bootstrap(monkeypatch)
    with TestClient(app) as client:
        r, secret = _bootstrap_owner(client)
        assert r.status_code == 303
        with db.connect() as conn:
            u = conn.execute("SELECT role, active FROM admin_users WHERE username_lc='owner1'").fetchone()
        assert u and u["role"] == "owner" and u["active"] == 1
        # wrong code is rejected
        assert client.post("/admin/login", data={"username": "owner1", "password": "supersecret123", "code": "000000"},
                           follow_redirects=False).status_code == 401
        # right password + code logs in and opens the owner panel
        good = client.post("/admin/login", data={"username": "owner1", "password": "supersecret123", "code": _code(secret)},
                           follow_redirects=False)
        assert good.status_code == 303 and "yb_admin" in good.headers.get("set-cookie", "")
        panel = client.get("/admin").text
        assert "Status admin" in panel and "Staff accounts" in panel


def test_bootstrap_ignored_once_owner_exists(monkeypatch):
    _enable_bootstrap(monkeypatch)
    with TestClient(app) as client:
        _bootstrap_owner(client)
        # second bootstrap attempt → link is now invalid
        r = client.get(f"/admin/setup?token={BOOT}")
        assert "Link expired" in r.text or r.status_code == 400


def test_owner_invites_staff_who_sets_up_and_logs_in(monkeypatch):
    _enable_bootstrap(monkeypatch)
    with TestClient(app) as client:
        _, osecret = _bootstrap_owner(client)
        client.post("/admin/login", data={"username": "owner1", "password": "supersecret123", "code": _code(osecret)},
                    follow_redirects=False)
        inv = client.post("/admin/users", data={"username": "alice"}, follow_redirects=False)
        assert inv.status_code == 303
        token = inv.headers["location"].split("invited:")[1]
        # staff completes setup with their own password + their own authenticator
        ssecret = aa.new_totp_secret()
        assert _staff_setup(client, token, "alicepass123", ssecret).status_code == 303
        al = client.post("/admin/login", data={"username": "alice", "password": "alicepass123", "code": _code(ssecret)},
                         follow_redirects=False)
        assert al.status_code == 303


def test_staff_cannot_manage_users(monkeypatch):
    _enable_bootstrap(monkeypatch)
    with TestClient(app) as client:
        _, osecret = _bootstrap_owner(client)
        client.post("/admin/login", data={"username": "owner1", "password": "supersecret123", "code": _code(osecret)},
                    follow_redirects=False)
        token = client.post("/admin/users", data={"username": "bob"}, follow_redirects=False).headers["location"].split("invited:")[1]
        ssecret = aa.new_totp_secret()
        _staff_setup(client, token, "bobpass12345", ssecret)
        client.post("/admin/logout", follow_redirects=False)
        client.post("/admin/login", data={"username": "bob", "password": "bobpass12345", "code": _code(ssecret)},
                    follow_redirects=False)
        # staff can edit causes but not create users
        assert client.post("/admin/users", data={"username": "carol"}, follow_redirects=False).status_code == 403


def test_cause_edit_requires_login_and_then_renders(monkeypatch):
    _enable_bootstrap(monkeypatch)
    with db.connect() as conn:
        started = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        inc = int(conn.execute("INSERT INTO incidents(service_name, started_at, resolved) VALUES ('Gateway',?,1)",
                               (started,)).lastrowid)
    with TestClient(app) as client:
        assert client.post(f"/admin/incident/{inc}/cause-form", data={"cause": "x"}, follow_redirects=False).status_code == 401
        _, osecret = _bootstrap_owner(client)
        client.post("/admin/login", data={"username": "owner1", "password": "supersecret123", "code": _code(osecret)},
                    follow_redirects=False)
        client.post(f"/admin/incident/{inc}/cause-form", data={"cause": "Bad deploy; rolled back."}, follow_redirects=False)
        with db.connect() as conn:
            assert conn.execute("SELECT cause FROM incidents WHERE id=?", (inc,)).fetchone()["cause"] == "Bad deploy; rolled back."
        assert "Bad deploy; rolled back." in client.get("/").text


def test_lockout_after_repeated_failures(monkeypatch):
    _enable_bootstrap(monkeypatch)
    with TestClient(app) as client:
        _bootstrap_owner(client)
        for _ in range(5):
            client.post("/admin/login", data={"username": "owner1", "password": "nope", "code": "000000"}, follow_redirects=False)
        r = client.post("/admin/login", data={"username": "owner1", "password": "nope", "code": "000000"}, follow_redirects=False)
        assert r.status_code == 429  # locked


def test_setup_get_renders_with_qr(monkeypatch):
    _enable_bootstrap(monkeypatch)
    with TestClient(app) as client:
        r = client.get(f"/admin/setup?token={BOOT}")
    assert r.status_code == 200
    assert "Set up your account" in r.text
    # QR (segno) renders inline when installed; manual key always shown
    assert "<svg" in r.text or "Manual key" in r.text


def test_disabled_without_secret(monkeypatch):
    monkeypatch.setenv("ADMIN_HMAC_SECRET", "")
    reset_settings()
    with TestClient(app) as client:
        assert "disabled" in client.get("/admin").text.lower()
