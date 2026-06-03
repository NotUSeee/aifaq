"""Account-based admin panel (/admin).

Owner bootstraps via ADMIN_BOOTSTRAP_TOKEN, then creates staff accounts; each
person sets their own password and enrolls a TOTP authenticator through a
one-time setup link. Login requires username + password + a rotating 6-digit
code. Sessions are HMAC-signed HttpOnly/SameSite=Strict cookies. The HMAC CLI
(announce.sh) remains as an out-of-band break-glass.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address

from .. import admin_auth as aa
from .. import db
from ..aggregator import incidents_recent
from ..config import get_settings

router = APIRouter(prefix="/admin")
_limiter = Limiter(key_func=get_remote_address)
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

COOKIE = "yb_admin"
SESSION_TTL = 12 * 3600          # 12h, then re-login + 2FA
SETUP_TTL_DAYS = 7
MAX_FAILS = 5
LOCK_MINUTES = 15
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


# ── helpers ─────────────────────────────────────────────────────────────
def _secret() -> str:
    return (get_settings().admin_hmac_secret or "").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _public_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _is_https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto", "").lower() == "https"
            or request.url.scheme == "https")


def _row(cur):
    r = cur.fetchone()
    return dict(r) if r else None


def _user_by_username(conn, username: str):
    return _row(conn.execute("SELECT * FROM admin_users WHERE username_lc=?", (username.lower(),)))


def _user_by_id(conn, uid: int):
    return _row(conn.execute("SELECT * FROM admin_users WHERE id=?", (uid,)))


def _user_by_setup(conn, token: str):
    return _row(conn.execute("SELECT * FROM admin_users WHERE setup_token=?", (token,)))


def _owner_exists(conn) -> bool:
    return _row(conn.execute("SELECT 1 AS x FROM admin_users WHERE role='owner' AND active=1")) is not None


def _current_user(request: Request):
    sec = _secret()
    uid = aa.session_uid(sec, request.cookies.get(COOKIE)) if sec else None
    if not uid:
        return None
    with db.connect() as conn:
        u = _user_by_id(conn, uid)
    return u if (u and u["active"]) else None


def _set_session(resp, request: Request, uid: int) -> None:
    resp.set_cookie(COOKIE, aa.make_session(_secret(), uid, SESSION_TTL), max_age=SESSION_TTL,
                    httponly=True, secure=_is_https(request), samesite="strict", path="/admin")


# ── panel ───────────────────────────────────────────────────────────────
@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@_limiter.limit("60/minute")
async def admin_home(request: Request, msg: str | None = None):
    if not _secret():
        return templates.TemplateResponse(request, "admin.html", {"request": request, "mode": "disabled"})
    user = _current_user(request)
    if not user:
        return templates.TemplateResponse(request, "admin.html", {"request": request, "mode": "login", "msg": msg})
    ctx = {
        "request": request, "mode": "panel", "user": user, "msg": msg,
        "public_base": _public_base(request),
        "incidents": incidents_recent(days=30, max_count=50),
        "announcements": _active_announcements(),
    }
    if user["role"] == "owner":
        with db.connect() as conn:
            ctx["users"] = [dict(r) for r in conn.execute(
                "SELECT id, username, role, active, setup_token, last_login_at FROM admin_users ORDER BY role, username_lc"
            ).fetchall()]
    return templates.TemplateResponse(request, "admin.html", ctx)


@router.post("/login", include_in_schema=False)
@_limiter.limit("10/minute")
async def admin_login(request: Request, username: str = Form(...), password: str = Form(...), code: str = Form("")):
    if not _secret():
        raise HTTPException(status_code=503, detail="admin disabled")
    err = templates.TemplateResponse(
        request, "admin.html", {"request": request, "mode": "login", "error": "Invalid username, password, or code."},
        status_code=401)
    with db.connect() as conn:
        u = _user_by_username(conn, username)
        if not u or not u["active"]:
            return err
        if u["locked_until"] and u["locked_until"] > _now_iso():
            return templates.TemplateResponse(
                request, "admin.html",
                {"request": request, "mode": "login", "error": "Account temporarily locked. Try again shortly."},
                status_code=429)
        pw_ok = aa.verify_password(password, u["password_salt"], u["password_hash"])
        totp_ok, step = aa.verify_totp(u["totp_secret"], code, u["last_totp_step"])
        if not (pw_ok and totp_ok):
            fails = (u["failed_logins"] or 0) + 1
            locked = _iso_in(LOCK_MINUTES * 60) if fails >= MAX_FAILS else None
            conn.execute("UPDATE admin_users SET failed_logins=?, locked_until=? WHERE id=?", (fails, locked, u["id"]))
            return err
        conn.execute(
            "UPDATE admin_users SET failed_logins=0, locked_until=NULL, last_totp_step=?, last_login_at=? WHERE id=?",
            (step, _now_iso(), u["id"]))
        uid = u["id"]
    resp = RedirectResponse("/admin", status_code=303)
    _set_session(resp, request, uid)
    return resp


@router.post("/logout", include_in_schema=False)
async def admin_logout(request: Request):
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(COOKIE, path="/admin")
    return resp


# ── one-time account setup (password + TOTP enrollment) ─────────────────
@router.get("/setup", response_class=HTMLResponse, include_in_schema=False)
@_limiter.limit("30/minute")
async def admin_setup_get(request: Request, token: str = ""):
    sec = _secret()
    if not sec:
        return templates.TemplateResponse(request, "admin.html", {"request": request, "mode": "disabled"})
    mode, username = _setup_mode(token)
    if mode is None:
        return templates.TemplateResponse(
            request, "admin_setup.html", {"request": request, "invalid": True}, status_code=400)
    totp_secret = aa.new_totp_secret()
    uri = aa.otpauth_uri(totp_secret, username or "owner")
    return templates.TemplateResponse(request, "admin_setup.html", {
        "request": request, "invalid": False, "mode": mode, "token": token, "username": username,
        "totp_secret": totp_secret, "secret_field": aa.sign_secret_field(sec, totp_secret, token),
        "otpauth_uri": uri, "qr_svg": aa.qr_svg(uri),
    })


@router.post("/setup", include_in_schema=False)
@_limiter.limit("10/minute")
async def admin_setup_post(request: Request, token: str = Form(...), username: str = Form(""),
                           password: str = Form(...), password2: str = Form(...),
                           secret_field: str = Form(...), code: str = Form("")):
    sec = _secret()
    if not sec:
        raise HTTPException(status_code=503, detail="admin disabled")
    mode, fixed_username = _setup_mode(token)
    if mode is None:
        return _setup_err(request, token, username, "This setup link is invalid or has expired.")
    totp_secret = aa.unsign_secret_field(sec, secret_field, token)
    if not totp_secret:
        return _setup_err(request, token, username, "Setup form tampered or stale — reload and try again.")
    if len(password) < 10:
        return _setup_err(request, token, username, "Password must be at least 10 characters.", totp_secret)
    if password != password2:
        return _setup_err(request, token, username, "Passwords don't match.", totp_secret)
    ok, step = aa.verify_totp(totp_secret, code)
    if not ok:
        return _setup_err(request, token, username, "That code didn't match — rescan the QR and enter a fresh code.", totp_secret)
    pw_hash, salt = aa.hash_password(password)

    with db.connect() as conn:
        if mode == "bootstrap":
            uname = (username or "").strip()
            if not _USERNAME_RE.match(uname):
                return _setup_err(request, token, username, "Username must be 3–32 chars (letters, numbers, . _ -).", totp_secret)
            if _user_by_username(conn, uname):
                return _setup_err(request, token, username, "That username is taken.", totp_secret)
            conn.execute(
                "INSERT INTO admin_users(username, username_lc, role, password_hash, password_salt, totp_secret, active, last_totp_step) "
                "VALUES (?,?, 'owner', ?,?,?, 1, ?)", (uname, uname.lower(), pw_hash, salt, totp_secret, step))
        else:  # staff completing their invite
            u = _user_by_setup(conn, token)
            if not u:
                return _setup_err(request, token, username, "This setup link is no longer valid.")
            conn.execute(
                "UPDATE admin_users SET password_hash=?, password_salt=?, totp_secret=?, active=1, "
                "setup_token=NULL, setup_expires=NULL, last_totp_step=?, failed_logins=0, locked_until=NULL WHERE id=?",
                (pw_hash, salt, totp_secret, step, u["id"]))
    return RedirectResponse("/admin?msg=setup_done", status_code=303)


# ── owner: user management ──────────────────────────────────────────────
@router.post("/users", include_in_schema=False)
@_limiter.limit("30/minute")
async def admin_users_create(request: Request, username: str = Form(...)):
    _require_owner(request)
    uname = (username or "").strip()
    if not _USERNAME_RE.match(uname):
        return RedirectResponse("/admin?msg=bad_username", status_code=303)
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        if _user_by_username(conn, uname):
            return RedirectResponse("/admin?msg=dup_username", status_code=303)
        conn.execute(
            "INSERT INTO admin_users(username, username_lc, role, active, setup_token, setup_expires) "
            "VALUES (?,?, 'staff', 0, ?, ?)", (uname, uname.lower(), token, _iso_in(SETUP_TTL_DAYS * 86400)))
    return RedirectResponse(f"/admin?msg=invited:{token}", status_code=303)


@router.post("/users/{uid}/reset", include_in_schema=False)
@_limiter.limit("30/minute")
async def admin_users_reset(request: Request, uid: int):
    _require_owner(request)
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        u = _user_by_id(conn, uid)
        if not u:
            raise HTTPException(status_code=404)
        conn.execute("UPDATE admin_users SET active=0, password_hash=NULL, password_salt=NULL, totp_secret=NULL, "
                     "setup_token=?, setup_expires=?, failed_logins=0, locked_until=NULL WHERE id=?",
                     (token, _iso_in(SETUP_TTL_DAYS * 86400), uid))
    return RedirectResponse(f"/admin?msg=invited:{token}", status_code=303)


@router.post("/users/{uid}/delete", include_in_schema=False)
@_limiter.limit("30/minute")
async def admin_users_delete(request: Request, uid: int):
    me = _require_owner(request)
    if me["id"] == uid:
        return RedirectResponse("/admin?msg=cant_delete_self", status_code=303)
    with db.connect() as conn:
        u = _user_by_id(conn, uid)
        if u and u["role"] == "owner" and not _row(conn.execute(
                "SELECT 1 AS x FROM admin_users WHERE role='owner' AND active=1 AND id<>?", (uid,))):
            return RedirectResponse("/admin?msg=last_owner", status_code=303)
        conn.execute("DELETE FROM admin_users WHERE id=?", (uid,))
    return RedirectResponse("/admin?msg=deleted", status_code=303)


# ── panel actions (incidents + announcements) ───────────────────────────
@router.post("/incident/{incident_id}/cause-form", include_in_schema=False)
@_limiter.limit("60/minute")
async def admin_cause_form(request: Request, incident_id: int, cause: str = Form("")):
    _require_user(request)
    cause = cause.strip()[:4000]
    with db.connect() as conn:
        if not _row(conn.execute("SELECT 1 AS x FROM incidents WHERE id=?", (incident_id,))):
            raise HTTPException(status_code=404, detail="incident not found")
        if cause:
            conn.execute("UPDATE incidents SET cause=?, cause_at=? WHERE id=?", (cause, _now_iso(), incident_id))
        else:
            conn.execute("UPDATE incidents SET cause=NULL, cause_at=NULL WHERE id=?", (incident_id,))
    return RedirectResponse("/admin", status_code=303)


@router.post("/announce-form", include_in_schema=False)
@_limiter.limit("30/minute")
async def admin_announce_form(request: Request, type: str = Form(...), severity: str = Form(...),
                              title: str = Form(...), body: str = Form(...)):
    _require_user(request)
    if type not in ("maintenance", "incident") or severity not in ("info", "warning", "critical"):
        raise HTTPException(status_code=422, detail="bad type/severity")
    title, body = title.strip()[:200], body.strip()[:4000]
    if not title or not body:
        raise HTTPException(status_code=422, detail="title and body required")
    with db.connect() as conn:
        conn.execute("INSERT INTO announcements(type, severity, title, body) VALUES (?,?,?,?)",
                     (type, severity, title, body))
    return RedirectResponse("/admin", status_code=303)


@router.post("/announce/{ann_id}/resolve-form", include_in_schema=False)
@_limiter.limit("30/minute")
async def admin_resolve_form(request: Request, ann_id: int):
    _require_user(request)
    with db.connect() as conn:
        row = _row(conn.execute("SELECT id, resolved_at FROM announcements WHERE id=?", (ann_id,)))
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        if not row["resolved_at"]:
            conn.execute("UPDATE announcements SET resolved_at=? WHERE id=?", (_now_iso(), ann_id))
            conn.execute("INSERT INTO announcement_updates(announcement_id, status, body) "
                         "VALUES (?, 'resolved', 'Resolved.')", (ann_id,))
    return RedirectResponse("/admin", status_code=303)


# ── small internals ─────────────────────────────────────────────────────
def _iso_in(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _setup_mode(token: str):
    """Return (mode, username) for a setup token, or (None, None) if invalid.
    mode is 'bootstrap' (create the first owner) or 'staff'."""
    if not token:
        return (None, None)
    s = get_settings()
    boot = (s.admin_bootstrap_token or "").strip()
    with db.connect() as conn:
        if boot and secrets_compare(token, boot) and not _owner_exists(conn):
            return ("bootstrap", None)
        u = _user_by_setup(conn, token)
        if u and not u["active"] and (not u["setup_expires"] or u["setup_expires"] > _now_iso()):
            return ("staff", u["username"])
    return (None, None)


def secrets_compare(a: str, b: str) -> bool:
    import hmac as _h
    return _h.compare_digest(a, b)


def _setup_err(request: Request, token: str, username: str, error: str, totp_secret: str | None = None):
    sec = _secret()
    mode, fixed = _setup_mode(token)
    totp_secret = totp_secret or aa.new_totp_secret()
    uri = aa.otpauth_uri(totp_secret, fixed or username or "owner")
    return templates.TemplateResponse(request, "admin_setup.html", {
        "request": request, "invalid": mode is None, "mode": mode or "staff", "token": token,
        "username": fixed or username, "error": error, "totp_secret": totp_secret,
        "secret_field": aa.sign_secret_field(sec, totp_secret, token),
        "otpauth_uri": uri, "qr_svg": aa.qr_svg(uri),
    }, status_code=400)


def _active_announcements() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, type, severity, title, body, created_at FROM announcements "
            "WHERE resolved_at IS NULL ORDER BY created_at DESC").fetchall()]


def _require_user(request: Request) -> dict:
    u = _current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="not signed in")
    return u


def _require_owner(request: Request) -> dict:
    u = _require_user(request)
    if u["role"] != "owner":
        raise HTTPException(status_code=403, detail="owner only")
    return u
