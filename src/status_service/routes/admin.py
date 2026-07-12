from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from .. import db, subscribers
from ..config import get_settings
from ..ratelimit import limiter as _limiter


def _announce_kind(ann_type: str, scheduled: bool = False) -> str:
    if ann_type == "maintenance":
        return "Scheduled maintenance" if scheduled else "Maintenance"
    return "Incident"

router = APIRouter(prefix="/admin")

REPLAY_WINDOW_SECONDS = 300  # 5 min — request timestamp must be within this window


class AnnouncePayload(BaseModel):
    type: str = Field(..., pattern="^(maintenance|incident)$")
    severity: str = Field(..., pattern="^(info|warning|critical)$")
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=4000)
    # Maintenance-window schedule (ISO-8601 UTC). A future starts_at shows
    # the entry under "Scheduled maintenance"; ends_at auto-resolves it.
    starts_at: str | None = Field(default=None)
    ends_at: str | None = Field(default=None)


def _normalize_window(payload: AnnouncePayload) -> tuple[str | None, str | None]:
    """Validate/normalize the optional maintenance window to UTC 'Z' ISO
    strings (the DB's canonical timestamp format, string-comparable)."""
    if payload.starts_at is None and payload.ends_at is None:
        return None, None
    if payload.type != "maintenance":
        raise HTTPException(status_code=422, detail="schedule window is only valid for type=maintenance")

    def norm(value: str | None, field: str) -> str | None:
        if value is None:
            return None
        v = value.strip()
        try:
            dt = datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"{field} is not valid ISO-8601")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    starts = norm(payload.starts_at, "starts_at")
    ends = norm(payload.ends_at, "ends_at")
    if starts and ends and ends <= starts:
        raise HTTPException(status_code=422, detail="ends_at must be after starts_at")
    return starts, ends


class UpdatePayload(BaseModel):
    status: str = Field(..., pattern="^(investigating|identified|monitoring|resolved)$")
    body: str = Field(..., min_length=1, max_length=4000)


class CausePayload(BaseModel):
    cause: str = Field(..., min_length=1, max_length=4000)


def _parse_or_422(model: type[BaseModel], raw: bytes):
    """Validate an already-HMAC-verified raw body. Manual parsing (after
    reading the body for the signature) lives outside FastAPI's request
    path, so a bare ValidationError would surface as a 500 — convert it to
    a clean 422 instead."""
    try:
        return model.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid payload: {exc}")


def _verify_hmac(
    body: bytes,
    secret: str,
    timestamp: str | None,
    signature: str | None,
) -> None:
    """Constant-time HMAC verification with replay protection.

    The client signs `f'{timestamp}.{body}'` with HMAC-SHA256. We require
    the timestamp to be within REPLAY_WINDOW_SECONDS of now to reject
    captured-and-replayed requests."""
    if not secret:
        raise HTTPException(status_code=503, detail="admin disabled — ADMIN_HMAC_SECRET not configured")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="missing X-Status-Timestamp or X-Status-Signature")
    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid timestamp")
    now = int(time.time())
    if abs(now - ts) > REPLAY_WINDOW_SECONDS:
        raise HTTPException(status_code=401, detail="timestamp outside replay window")
    payload = f"{ts}.".encode("utf-8") + body
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="bad signature")


@router.post("/announce")
@_limiter.limit("30/minute")
async def announce(
    request: Request,
    x_status_timestamp: str | None = Header(default=None),
    x_status_signature: str | None = Header(default=None),
) -> JSONResponse:
    raw = await request.body()
    settings = get_settings()
    _verify_hmac(raw, settings.admin_hmac_secret, x_status_timestamp, x_status_signature)

    payload = _parse_or_422(AnnouncePayload, raw)
    starts_at, ends_at = _normalize_window(payload)
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO announcements(type, severity, title, body, starts_at, ends_at) VALUES (?,?,?,?,?,?)",
            (payload.type, payload.severity, payload.title, payload.body, starts_at, ends_at),
        )
        ann_id = cur.lastrowid
    await subscribers.broadcast_announcement(
        _announce_kind(payload.type, scheduled=bool(starts_at)),
        payload.severity, payload.title, payload.body)
    return JSONResponse({"ok": True, "id": ann_id})


@router.post("/announce/{ann_id}/update")
@_limiter.limit("30/minute")
async def announce_update(
    ann_id: int,
    request: Request,
    x_status_timestamp: str | None = Header(default=None),
    x_status_signature: str | None = Header(default=None),
) -> JSONResponse:
    raw = await request.body()
    settings = get_settings()
    _verify_hmac(raw, settings.admin_hmac_secret, x_status_timestamp, x_status_signature)

    payload = _parse_or_422(UpdatePayload, raw)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, type, severity, title, resolved_at FROM announcements WHERE id=?",
            (ann_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="announcement not found")
        if row["resolved_at"]:
            raise HTTPException(status_code=409, detail="announcement already resolved")
        conn.execute(
            "INSERT INTO announcement_updates(announcement_id, status, body) VALUES (?,?,?)",
            (ann_id, payload.status, payload.body),
        )
        if payload.status == "resolved":
            conn.execute(
                "UPDATE announcements SET resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (ann_id,),
            )
    await subscribers.broadcast_announcement(
        _announce_kind(row["type"]), row["severity"], row["title"],
        f"[{payload.status}] {payload.body}")
    return JSONResponse({"ok": True})


@router.post("/announce/{ann_id}/resolve")
@_limiter.limit("30/minute")
async def announce_resolve(
    ann_id: int,
    request: Request,
    x_status_timestamp: str | None = Header(default=None),
    x_status_signature: str | None = Header(default=None),
) -> JSONResponse:
    raw = await request.body()
    settings = get_settings()
    _verify_hmac(raw, settings.admin_hmac_secret, x_status_timestamp, x_status_signature)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, type, severity, title, resolved_at FROM announcements WHERE id=?",
            (ann_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="announcement not found")
        if row["resolved_at"]:
            return JSONResponse({"ok": True, "already_resolved": True})
        now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("UPDATE announcements SET resolved_at = ? WHERE id=?", (now_iso, ann_id))
        conn.execute(
            "INSERT INTO announcement_updates(announcement_id, status, body) VALUES (?, 'resolved', 'Resolved.')",
            (ann_id,),
        )
    await subscribers.broadcast_announcement(
        _announce_kind(row["type"]), row["severity"], row["title"], "Resolved.")
    return JSONResponse({"ok": True})


@router.post("/incident/{incident_id}/cause")
@_limiter.limit("30/minute")
async def set_incident_cause(
    incident_id: int,
    request: Request,
    x_status_timestamp: str | None = Header(default=None),
    x_status_signature: str | None = Header(default=None),
) -> JSONResponse:
    """Attach (or overwrite) an admin-authored root-cause / post-mortem on an
    auto-detected incident, surfaced publicly as "Why this happened". Plain
    text — rendered HTML-escaped by Jinja. Idempotent: re-POST overwrites and
    refreshes cause_at. Allowed on open and resolved incidents."""
    raw = await request.body()
    settings = get_settings()
    _verify_hmac(raw, settings.admin_hmac_secret, x_status_timestamp, x_status_signature)

    payload = _parse_or_422(CausePayload, raw)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="incident not found")
        conn.execute(
            "UPDATE incidents SET cause=?, cause_at=? WHERE id=?",
            (payload.cause, now_iso, incident_id),
        )
    return JSONResponse({"ok": True, "id": incident_id})
