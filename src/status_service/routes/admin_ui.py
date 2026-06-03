"""Browser-based admin panel (/admin) — a friendlier alternative to the
HMAC CLI for editing incident root-causes and posting announcements.

Auth model: a single admin password (ADMIN_PASSWORD, falling back to
ADMIN_HMAC_SECRET) exchanged at /admin/login for a signed, HttpOnly,
SameSite=Strict session cookie. SameSite=Strict is the CSRF defense (the
cookie isn't sent on cross-site POSTs); the login is rate-limited and the
password compared in constant time. State-changing actions re-check the
session. No new privilege is introduced — the password is the same trust
level as the existing HMAC secret.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address

from .. import db
from ..aggregator import incidents_recent
from ..config import get_settings

router = APIRouter(prefix="/admin")
_limiter = Limiter(key_func=get_remote_address)

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

COOKIE = "yb_admin"
SESSION_TTL = 7 * 24 * 3600  # 7 days


def _admin_password() -> str:
    s = get_settings()
    return (s.admin_password or s.admin_hmac_secret or "").strip()


def _sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _make_token(secret: str) -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    return exp + "." + _sign(secret, exp)


def _token_valid(secret: str, token: str | None) -> bool:
    if not secret or not token or "." not in token:
        return False
    exp, sig = token.rsplit(".", 1)
    if not exp.isdigit() or int(exp) < int(time.time()):
        return False
    return hmac.compare_digest(_sign(secret, exp), sig)


def _authed(request: Request) -> bool:
    pw = _admin_password()
    return bool(pw) and _token_valid(pw, request.cookies.get(COOKIE))


def _is_https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto", "").lower() == "https"
            or request.url.scheme == "https")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _active_announcements() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, type, severity, title, body, created_at FROM announcements "
            "WHERE resolved_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def _require_auth(request: Request) -> None:
    if not _authed(request):
        raise HTTPException(status_code=401, detail="not signed in")


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@_limiter.limit("60/minute")
async def admin_home(request: Request):
    if not _admin_password():
        return templates.TemplateResponse(request, "admin.html", {"request": request, "mode": "disabled"})
    if not _authed(request):
        return templates.TemplateResponse(request, "admin.html", {"request": request, "mode": "login"})
    return templates.TemplateResponse(request, "admin.html", {
        "request": request,
        "mode": "panel",
        "incidents": incidents_recent(days=30, max_count=50),
        "announcements": _active_announcements(),
    })


@router.post("/login", include_in_schema=False)
@_limiter.limit("10/minute")
async def admin_login(request: Request, password: str = Form(...)):
    pw = _admin_password()
    if not pw:
        raise HTTPException(status_code=503, detail="admin disabled")
    if not hmac.compare_digest(password, pw):
        return templates.TemplateResponse(
            request, "admin.html",
            {"request": request, "mode": "login", "error": "Incorrect password."},
            status_code=401,
        )
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(COOKIE, _make_token(pw), max_age=SESSION_TTL, httponly=True,
                    secure=_is_https(request), samesite="strict", path="/admin")
    return resp


@router.post("/logout", include_in_schema=False)
async def admin_logout(request: Request):
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(COOKIE, path="/admin")
    return resp


@router.post("/incident/{incident_id}/cause-form", include_in_schema=False)
@_limiter.limit("60/minute")
async def admin_cause_form(request: Request, incident_id: int, cause: str = Form("")):
    _require_auth(request)
    cause = cause.strip()[:4000]
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="incident not found")
        if cause:
            conn.execute("UPDATE incidents SET cause=?, cause_at=? WHERE id=?",
                         (cause, _now_iso(), incident_id))
        else:  # empty box clears the reason
            conn.execute("UPDATE incidents SET cause=NULL, cause_at=NULL WHERE id=?", (incident_id,))
    return RedirectResponse("/admin", status_code=303)


@router.post("/announce-form", include_in_schema=False)
@_limiter.limit("30/minute")
async def admin_announce_form(request: Request, type: str = Form(...), severity: str = Form(...),
                              title: str = Form(...), body: str = Form(...)):
    _require_auth(request)
    if type not in ("maintenance", "incident") or severity not in ("info", "warning", "critical"):
        raise HTTPException(status_code=422, detail="bad type/severity")
    title = title.strip()[:200]
    body = body.strip()[:4000]
    if not title or not body:
        raise HTTPException(status_code=422, detail="title and body required")
    with db.connect() as conn:
        conn.execute("INSERT INTO announcements(type, severity, title, body) VALUES (?,?,?,?)",
                     (type, severity, title, body))
    return RedirectResponse("/admin", status_code=303)


@router.post("/announce/{ann_id}/resolve-form", include_in_schema=False)
@_limiter.limit("30/minute")
async def admin_resolve_form(request: Request, ann_id: int):
    _require_auth(request)
    with db.connect() as conn:
        row = conn.execute("SELECT id, resolved_at FROM announcements WHERE id=?", (ann_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        if not row["resolved_at"]:
            conn.execute("UPDATE announcements SET resolved_at=? WHERE id=?", (_now_iso(), ann_id))
            conn.execute(
                "INSERT INTO announcement_updates(announcement_id, status, body) "
                "VALUES (?, 'resolved', 'Resolved.')", (ann_id,))
    return RedirectResponse("/admin", status_code=303)
