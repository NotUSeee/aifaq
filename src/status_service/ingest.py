"""Reports that are SENT to this service instead of fetched by it.

Three kinds, each signed with its own secret:

* The platform's own health report, pushed by its checker once a minute.
  It carries what GET /status/api and /status/api/shards return, but it does
  not travel through the website. So it keeps arriving when the website is
  down, and the page can still say whether the bot is working, which is the
  part most people care about.

* Website checks made from other places ("vantage points"). One machine on
  one connection cannot tell "yourbot.gg is down" from "my own route to it
  is down". Several on unrelated networks can.

* "A release is going out" and "it is done", from the deploy pipeline. A
  restart during a release is expected and short. Without this the page shows
  it as a failure nobody explains.

Only the latest report of each sender is kept (meta_kv). The scheduler reads
them at its next cycle, so stored history keeps its shape of one row per
service per minute however a reading arrived. Releases are the exception:
each one is a row of its own (`releases`), because an incident is tied to the
release it began in for as long as that incident is listed.

Signing is the scheme the admin API already uses: HMAC-SHA256 over
``"<unix seconds>." + body``, hex, in X-Status-Signature, with the seconds in
X-Status-Timestamp.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone

from . import db

REPLAY_WINDOW_SECONDS = 120
MAX_BODY_BYTES = 256 * 1024

# A report older than this no longer describes "now". Senders report every
# minute, so this allows one missed report and some jitter.
PLATFORM_FRESH_SECONDS = 150
VANTAGE_FRESH_SECONDS = 100

PLATFORM_KEY = "ingest.platform"
VANTAGE_PREFIX = "ingest.vantage."
MAX_VANTAGE_POINTS = 8
_VANTAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
_VANTAGE_STATES = ("operational", "down")


class Rejected(Exception):
    """A report that must not be stored. `status` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def verify(secret: str, timestamp: str | None, signature: str | None, body: bytes, now: float | None = None) -> int:
    """Check the signature and the age of a report. Returns its timestamp.

    A blank or short secret means the route is switched off: the caller
    answers as if it did not exist."""
    if not secret or len(secret) < 32:
        raise Rejected(404, "not found")
    if len(body) > MAX_BODY_BYTES:
        raise Rejected(413, "report too large")
    if not timestamp or not signature:
        raise Rejected(401, "missing X-Status-Timestamp or X-Status-Signature")
    try:
        ts = int(timestamp)
    except ValueError:
        raise Rejected(401, "invalid timestamp") from None
    if abs((now if now is not None else time.time()) - ts) > REPLAY_WINDOW_SECONDS:
        raise Rejected(401, "timestamp outside the accepted window")
    expected = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise Rejected(401, "bad signature")
    return ts


def sign(secret: str, body: bytes, ts: int | None = None) -> dict[str, str]:
    """The headers a sender attaches. Used by tests and by the reference senders."""
    ts = int(time.time()) if ts is None else int(ts)
    digest = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    return {"X-Status-Timestamp": str(ts), "X-Status-Signature": digest}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso[:-1] + "+00:00" if iso.endswith("Z") else iso)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).total_seconds()


def _load(key: str) -> dict | None:
    raw = db.kv_get(key)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# ── The platform's own report ─────────────────────────────────────────────

def store_platform_report(payload: dict, ts: int) -> None:
    """Keep the newest report. One that was signed before the one already
    held is dropped: a delayed or replayed request must not turn the clock
    back."""
    status_body = payload.get("status")
    if not isinstance(status_body, dict) or not isinstance(status_body.get("current"), (dict, list)):
        raise Rejected(422, "status.current is required")
    shards = payload.get("shards")
    if shards is not None and not isinstance(shards, dict):
        raise Rejected(422, "shards must be an object")
    held = _load(PLATFORM_KEY)
    if held and int(held.get("ts") or 0) > ts:
        raise Rejected(409, "a newer report is already held")
    db.kv_set(PLATFORM_KEY, json.dumps(
        {"ts": ts, "received_at": _now_iso(), "status": status_body, "shards": shards},
        separators=(",", ":")))


def fresh_platform_report() -> dict | None:
    """The held platform report, if it is recent enough to describe now."""
    held = _load(PLATFORM_KEY)
    if not held:
        return None
    age = _age_seconds(held.get("received_at"))
    if age is None or age > PLATFORM_FRESH_SECONDS or age < -REPLAY_WINDOW_SECONDS:
        return None
    return held


def platform_report_age() -> float | None:
    held = _load(PLATFORM_KEY)
    return _age_seconds(held.get("received_at")) if held else None


# ── Website checks from other places ──────────────────────────────────────

def store_vantage_report(payload: dict, ts: int) -> str:
    name = str(payload.get("vantage") or "").strip().lower()
    if not _VANTAGE_NAME.match(name):
        raise Rejected(422, "vantage must be 2 to 32 characters: a to z, 0 to 9 and dashes")
    status = payload.get("status")
    if status not in _VANTAGE_STATES:
        raise Rejected(422, "status must be operational or down")
    key = VANTAGE_PREFIX + name
    held = _load(key)
    if held and int(held.get("ts") or 0) > ts:
        raise Rejected(409, "a newer report is already held")
    if held is None and len(_vantage_keys()) >= MAX_VANTAGE_POINTS:
        raise Rejected(409, "too many vantage points")

    def _int(value) -> int | None:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    label = str(payload.get("label") or "").strip()[:40] or name
    error = payload.get("error")
    db.kv_set(key, json.dumps({
        "ts": ts, "received_at": _now_iso(), "vantage": name, "label": label, "status": status,
        "http_status": _int(payload.get("http_status")), "response_ms": _int(payload.get("response_ms")),
        "error": str(error)[:200] if error else None,
    }, separators=(",", ":")))
    return name


OWN_SITE_KEY = "monitor.own_site_check"


def store_own_site_check(status: str, response_ms: int | None) -> None:
    """What this service's own request saw, before the other places were
    asked. Kept apart from the stored verdict so the page can name every
    place and what each one saw."""
    db.kv_set(OWN_SITE_KEY, json.dumps(
        {"received_at": _now_iso(), "status": status, "response_ms": response_ms}, separators=(",", ":")))


def places(active_within: float = 600.0) -> list[dict]:
    """Every place the website is checked from, ours first, with what each
    saw last. A vantage point that has gone quiet for ten minutes is left
    out: the page must not claim a place that is no longer looking."""
    out: list[dict] = []
    own = _load(OWN_SITE_KEY)
    own_age = _age_seconds(own.get("received_at")) if own else None
    out.append({
        "name": "home", "label": "our own monitor",
        "status": (own or {}).get("status") if own_age is not None and own_age <= VANTAGE_FRESH_SECONDS else "unknown",
        "age_seconds": None if own_age is None else round(own_age, 1), "fresh": own_age is not None and own_age <= VANTAGE_FRESH_SECONDS,
    })
    for report in vantage_reports(fresh_only=False):
        if report["age_seconds"] > active_within:
            continue
        out.append({
            "name": report["vantage"], "label": report.get("label") or report["vantage"],
            "status": report["status"] if report["fresh"] else "unknown",
            "age_seconds": report["age_seconds"], "fresh": report["fresh"],
        })
    return out


def _vantage_keys() -> list[str]:
    with db.connect() as conn:
        rows = conn.execute("SELECT key FROM meta_kv WHERE key LIKE ? ORDER BY key", (VANTAGE_PREFIX + "%",)).fetchall()
    return [r["key"] for r in rows]


def vantage_reports(fresh_only: bool = True) -> list[dict]:
    """Latest report of each vantage point, with its age. By default only
    those recent enough to vote on the present."""
    out: list[dict] = []
    for key in _vantage_keys():
        held = _load(key)
        if not held:
            continue
        age = _age_seconds(held.get("received_at"))
        if age is None:
            continue
        held["age_seconds"] = round(age, 1)
        held["fresh"] = -REPLAY_WINDOW_SECONDS <= age <= VANTAGE_FRESH_SECONDS
        if held["fresh"] or not fresh_only:
            out.append(held)
    return out


# ── Releases, announced by the deploy pipeline ────────────────────────────

RELEASE_SEEN_KEY = "ingest.release.seen"
RELEASE_DEFAULT_MINUTES = 30
RELEASE_MIN_MINUTES = 5
RELEASE_MAX_MINUTES = 180
# An incident that begins this soon after a release ended still belongs to
# it: the platform's own checks need a minute or two to notice a process
# that did not come back.
RELEASE_AFTERMATH_SECONDS = 300
_RELEASE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_RELEASE_RESULTS = ("done", "failed")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso[:-1] + "+00:00" if iso.endswith("Z") else iso)


def _refuse_a_repeat(conn, signature: str, now: float) -> None:
    """Refuse a signed request that was already accepted, and remember this one.

    The platform's health report is guarded by order: one sender, so "signed
    before the one we hold" means stale. That does not work here. Two machines
    send release events (the build, and whoever runs the deploy script) and
    their clocks are not the same clock: a real "start" from the machine that
    runs a few seconds behind would be refused as old.

    What must not happen is the SAME request being accepted twice: a captured
    "start", sent again after the release ended, would put the notice back up.
    A signature covers the timestamp and stops being valid after
    REPLAY_WINDOW_SECONDS, so each one only has to be remembered that long.
    """
    row = conn.execute("SELECT value FROM meta_kv WHERE key=?", (RELEASE_SEEN_KEY,)).fetchone()
    try:
        seen = json.loads(row["value"]) if row else {}
    except ValueError:
        seen = {}
    if not isinstance(seen, dict):
        seen = {}
    horizon = now - REPLAY_WINDOW_SECONDS - 60
    seen = {sig: at for sig, at in seen.items() if isinstance(at, (int, float)) and at >= horizon}
    if signature in seen:
        raise Rejected(409, "this request was already received")
    seen[signature] = now
    conn.execute(
        "INSERT INTO meta_kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (RELEASE_SEEN_KEY, json.dumps(seen, separators=(",", ":"))))


def store_release_event(payload: dict, ts: int, signature: str | None = None) -> dict:
    """A release starting, or finishing.

        {"action": "start", "version": "prod-1a2b3c4", "expected_minutes": 25}
        {"action": "finish", "version": "prod-1a2b3c4", "result": "done"}

    Safe to send twice. A second "start" for the release already going out
    only moves its expiry, and a "finish" for one that is not open changes
    nothing. The very same signed request is accepted once only (`signature`,
    see _refuse_a_repeat), so a replayed "start" cannot put the notice back up
    after the release ended.

    `expected_minutes` is how long the notice may stay up without a "finish":
    a pipeline that dies must not leave "a release is going out" on the page
    for the rest of the day.
    """
    action = payload.get("action")
    if action not in ("start", "finish"):
        raise Rejected(422, "action must be start or finish")
    version = str(payload.get("version") or "").strip()
    if not _RELEASE_VERSION.match(version):
        raise Rejected(422, "version must be 1 to 64 characters: letters, digits, dot, dash, plus or underscore")
    result = payload.get("result", "done")
    if action == "finish" and result not in _RELEASE_RESULTS:
        raise Rejected(422, "result must be done or failed")
    minutes = payload.get("expected_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        minutes = RELEASE_DEFAULT_MINUTES
    minutes = min(RELEASE_MAX_MINUTES, max(RELEASE_MIN_MINUTES, int(minutes)))

    now = datetime.now(timezone.utc)
    now_iso = _iso(now)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if signature:
                _refuse_a_repeat(conn, signature, now.timestamp())
            open_rows = conn.execute(
                "SELECT id, version, expires_at FROM releases WHERE ended_at IS NULL ORDER BY id DESC").fetchall()
            going_out = next((r for r in open_rows if r["version"] == version and r["expires_at"] > now_iso), None)
            release_id: int | None = None
            repeated = False

            if action == "start":
                expires = _iso(now + timedelta(minutes=minutes))
                if going_out is not None:
                    release_id, repeated = int(going_out["id"]), True
                    conn.execute("UPDATE releases SET expires_at=? WHERE id=?",
                                 (max(expires, going_out["expires_at"]), release_id))
                else:
                    # Anything else still marked open was replaced by this
                    # release, or ran out its time without a word.
                    for row in open_rows:
                        ran_out = row["expires_at"] <= now_iso
                        conn.execute("UPDATE releases SET ended_at=?, result=? WHERE id=?",
                                     (row["expires_at"] if ran_out else now_iso,
                                      "expired" if ran_out else "replaced", row["id"]))
                    release_id = int(conn.execute(
                        "INSERT INTO releases(version, started_at, expires_at) VALUES (?,?,?)",
                        (version, now_iso, expires)).lastrowid)
            else:
                # The newest open release of this version, even one whose time
                # ran out: it overran, and now we know when it really ended.
                target = next((r for r in open_rows if r["version"] == version), None)
                if target is not None:
                    release_id = int(target["id"])
                    conn.execute("UPDATE releases SET ended_at=?, result=? WHERE id=?", (now_iso, result, release_id))

            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return {"action": action, "release": release_id, "repeated": repeated}


def current_release() -> dict | None:
    """The release going out right now, or None."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, version, started_at, expires_at FROM releases "
            "WHERE ended_at IS NULL AND expires_at > ? ORDER BY id DESC LIMIT 1", (_now_iso(),)).fetchone()
    return dict(row) if row else None


def release_windows(since: datetime) -> list[tuple[datetime, datetime]]:
    """(start, end) of every release that was going out at some point after
    `since`, oldest first. One still going out ends now. One that was never
    reported finished ends when its time ran out."""
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT started_at, expires_at, ended_at FROM releases "
            "WHERE COALESCE(ended_at, expires_at) >= ? ORDER BY started_at ASC", (_iso(since),)).fetchall()
    out: list[tuple[datetime, datetime]] = []
    for r in rows:
        try:
            start = _parse(r["started_at"])
            end = _parse(r["ended_at"]) if r["ended_at"] else min(now, _parse(r["expires_at"]))
        except ValueError:
            continue
        out.append((start, max(start, end)))
    return out


def release_at(moment: datetime, windows: list[tuple[datetime, datetime]]) -> str | None:
    """How `moment` (when an incident began) relates to a release: "during"
    one, just "after" one, or None. A statement about time, not about cause."""
    after = False
    for start, end in windows:
        if start <= moment <= end:
            return "during"
        if end < moment <= end + timedelta(seconds=RELEASE_AFTERMATH_SECONDS):
            after = True
    return "after" if after else None
