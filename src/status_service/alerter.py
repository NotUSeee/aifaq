from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .aggregator import SERVICE_GROUPS, latest_per_service, overall_status, sla_summary
from .config import Settings
from .probes import ProbeResult

logger = logging.getLogger("status_service.alerter")

COLOR_RED = 0xE05A5A
COLOR_GREEN = 0x6BCB8B
COLOR_AMBER = 0xE0A33E
COLOR_GRAY = 0x888888

SLA_DAILY_KEY = "sla_at_risk"
SSL_WARN_KEY_PREFIX = "ssl_warn_"
BOARD_MSG_KEY = "alert_board_message_id"
LAST_OVERALL_KEY = "alert_last_overall"
OUTAGE_PING_MSG_KEY = "alert_outage_ping_msg_id"
OUTAGE_PING_AT_KEY = "alert_outage_ping_at"

STATUS_EMOJI = {"operational": "🟢", "degraded": "🟡", "down": "🔴", "unknown": "⚪"}
OVERALL_META = {
    "operational":    ("🟢 All Systems Operational", COLOR_GREEN),
    "degraded":       ("🟡 Degraded Performance", COLOR_AMBER),
    "partial_outage": ("🟠 Partial Outage", COLOR_AMBER),
    "outage":         ("🔴 Major Outage", COLOR_RED),
    "unknown":        ("⚪ Status Unknown", COLOR_GRAY),
}


class Alerter:
    """Decides when to fire Discord webhook alerts based on probe results
    and aggregator state.

    Two styles (ALERT_STYLE):
    - "board" (default): ONE message in the channel, edited in place every
      time the state changes. A full-site outage is one red board, not a
      per-service message flood.
    - "stream": legacy event posts — suppresses flapping by waiting
      ALERT_THRESHOLD_MIN before firing and enforcing ALERT_COOLDOWN_MIN
      between alerts per service.

    SSL-expiry and SLA warnings post as separate messages in both styles
    (they are daily-capped pings you want to notice).
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self._client = client
        self._last_board_sig: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.alert_discord_webhook_url)

    async def evaluate(self, results: list[ProbeResult]) -> None:
        if not self.enabled:
            return
        if self.settings.alert_style == "board":
            await self._update_board()
        else:
            await self._evaluate_incidents()
        await self._evaluate_ssl(results)
        await self._evaluate_sla()

    # ── Status board (single edited message) ────────────────────────────
    def _build_board_embed(self, currents, overall: str) -> dict:
        """Render current state as one embed. Uses Discord's <t:..:R>
        relative timestamps for incident ages so the message stays live
        WITHOUT needing an edit every minute — edits only happen on
        actual state transitions."""
        headline, color = OVERALL_META.get(overall, OVERALL_META["unknown"])

        by_name = {c.name: c for c in currents}
        placed: set[str] = set()
        fields: list[dict] = []
        for title, names in SERVICE_GROUPS:
            lines = []
            for n in names:
                c = by_name.get(n)
                if c is None:
                    continue
                placed.add(n)
                lines.append(f"{STATUS_EMOJI.get(c.status, '⚪')} {c.name}")
            if lines:
                fields.append({"name": title, "value": "\n".join(lines), "inline": True})
        leftovers = [c for c in currents if c.name not in placed]
        if leftovers:
            fields.append({
                "name": "Other",
                "value": "\n".join(f"{STATUS_EMOJI.get(c.status, '⚪')} {c.name}" for c in leftovers),
                "inline": True,
            })

        with db.connect() as conn:
            open_incidents = conn.execute(
                "SELECT service_name, started_at FROM incidents WHERE resolved=0 ORDER BY started_at"
            ).fetchall()
        if open_incidents:
            lines = []
            for inc in open_incidents[:10]:
                try:
                    unix = int(_parse_iso(inc["started_at"]).timestamp())
                    lines.append(f"🔴 **{inc['service_name']}** — down since <t:{unix}:R>")
                except Exception:
                    lines.append(f"🔴 **{inc['service_name']}**")
            if len(open_incidents) > 10:
                lines.append(f"… and {len(open_incidents) - 10} more")
            fields.append({"name": "Ongoing incidents", "value": "\n".join(lines), "inline": False})

        return {
            "title": headline,
            "color": color,
            "url": self.settings.status_public_url,
            "fields": fields,
            "footer": {"text": "YourBot status · live board, updates on change"},
        }

    async def _update_board(self) -> None:
        currents = latest_per_service()
        overall = overall_status(currents)
        embed = self._build_board_embed(currents, overall)
        # Signature excludes the timestamp we stamp below — otherwise every
        # cycle would look "changed" and we'd edit once a minute for nothing.
        sig = json.dumps(embed, sort_keys=True)
        if sig != self._last_board_sig:
            embed["timestamp"] = datetime.now(timezone.utc).isoformat()
            if await self._board_send_or_edit(embed):
                self._last_board_sig = sig
        await self._outage_ping(overall)

    async def _outage_ping(self, overall: str) -> None:
        """Board mode is deliberately silent — this is the one exception.
        When overall status transitions INTO "outage", post a separate
        mention ping (ALERT_OUTAGE_MENTION); delete it again on recovery
        so the channel stays clean. Cooldown guards flapping."""
        mention = (self.settings.alert_outage_mention or "").strip()
        last = db.kv_get(LAST_OVERALL_KEY)
        if overall != last:
            db.kv_set(LAST_OVERALL_KEY, overall)
        if not mention:
            return
        url = self.settings.alert_discord_webhook_url

        if overall == "outage" and last != "outage":
            last_ping = db.kv_get(OUTAGE_PING_AT_KEY)
            if last_ping:
                try:
                    age = datetime.now(timezone.utc) - _parse_iso(last_ping)
                    if age < timedelta(minutes=self.settings.alert_cooldown_min):
                        return
                except Exception:
                    pass
            payload = {
                "content": f"{mention} 🔴 **Major outage** — {self.settings.status_public_url}",
                "allowed_mentions": {"parse": ["roles", "everyone", "users"]},
            }
            sep = "&" if "?" in url else "?"
            try:
                r = await self._client.post(f"{url}{sep}wait=true", json=payload, timeout=10.0)
                if r.status_code < 400:
                    msg_id = str((r.json() or {}).get("id") or "")
                    if msg_id:
                        db.kv_set(OUTAGE_PING_MSG_KEY, msg_id)
                    db.kv_set(OUTAGE_PING_AT_KEY, _to_iso(datetime.now(timezone.utc)))
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("outage ping failed: %s", exc)

        elif overall != "outage" and last == "outage":
            msg_id = db.kv_get(OUTAGE_PING_MSG_KEY)
            if not msg_id:
                return
            try:
                await self._client.delete(f"{url}/messages/{msg_id}", timeout=10.0)
            except httpx.HTTPError as exc:
                logger.warning("outage ping cleanup failed: %s", exc)
            db.kv_set(OUTAGE_PING_MSG_KEY, "")

    async def _board_send_or_edit(self, embed: dict) -> bool:
        """Edit the persisted board message; (re)create it when missing or
        deleted. Returns True when Discord accepted the payload."""
        url = self.settings.alert_discord_webhook_url
        payload = {"embeds": [embed]}
        msg_id = db.kv_get(BOARD_MSG_KEY)
        if msg_id:
            try:
                r = await self._client.patch(f"{url}/messages/{msg_id}", json=payload, timeout=10.0)
                if r.status_code == 404:
                    msg_id = None  # someone deleted the board — repost below
                elif r.status_code >= 400:
                    logger.warning("board edit returned %s", r.status_code)
                    return False
                else:
                    return True
            except httpx.HTTPError as exc:
                logger.warning("board edit failed: %s", exc)
                return False
        sep = "&" if "?" in url else "?"
        try:
            r = await self._client.post(f"{url}{sep}wait=true", json=payload, timeout=10.0)
            if r.status_code >= 400:
                logger.warning("board create returned %s", r.status_code)
                return False
            new_id = str((r.json() or {}).get("id") or "")
            if new_id:
                db.kv_set(BOARD_MSG_KEY, new_id)
            return True
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("board create failed: %s", exc)
            return False

    async def _evaluate_incidents(self) -> None:
        threshold = timedelta(minutes=self.settings.alert_threshold_min)
        cooldown = timedelta(minutes=self.settings.alert_cooldown_min)
        now = datetime.now(timezone.utc)

        with db.connect() as conn:
            open_incidents = conn.execute(
                "SELECT id, service_name, started_at FROM incidents WHERE resolved=0"
            ).fetchall()
            recently_resolved = conn.execute(
                """
                SELECT id, service_name, started_at, ended_at, duration_min
                FROM incidents
                WHERE resolved=1 AND ended_at >= ?
                ORDER BY ended_at DESC
                """,
                (_to_iso(now - timedelta(minutes=self.settings.alert_cooldown_min * 2)),),
            ).fetchall()
            states = {r["service_name"]: r for r in conn.execute(
                "SELECT service_name, last_alert_at, last_status FROM alert_state"
            ).fetchall()}

        for inc in open_incidents:
            name = inc["service_name"]
            started = _parse_iso(inc["started_at"])
            duration = now - started
            if duration < threshold:
                continue
            state = states.get(name)
            if state is not None and state["last_status"] == "down":
                last_alert = _parse_iso(state["last_alert_at"])
                if (now - last_alert) < cooldown:
                    continue
            await self._post_incident_alert(name, started, duration)
            self._record_alert(name, "down", now)

        for inc in recently_resolved:
            name = inc["service_name"]
            state = states.get(name)
            # Only announce recovery for services we alerted "down" on;
            # recording "resolved" here is also what stops a repeat
            # announcement on the next cycle.
            if state is None or state["last_status"] != "down":
                continue
            ended = _parse_iso(inc["ended_at"])
            if (now - ended) > cooldown:
                continue
            duration_min = inc["duration_min"] or 0
            await self._post_resolved(name, duration_min)
            self._record_alert(name, "resolved", now)

    async def _evaluate_ssl(self, results: list[ProbeResult]) -> None:
        ssl_result = next((r for r in results if r.service_name == "SSL Certificate"), None)
        if ssl_result is None or not ssl_result.extra:
            return
        days_left = ssl_result.extra.get("days_left")
        if days_left is None:
            return
        if days_left < self.settings.ssl_critical_days:
            tier = "critical"
        elif days_left < self.settings.ssl_warn_days:
            tier = "warn"
        else:
            return

        key = f"{SSL_WARN_KEY_PREFIX}{tier}"
        if not self._daily_alert_due(key):
            return
        await self._post_ssl_warning(days_left, tier)
        self._mark_daily_alert(key)

    async def _evaluate_sla(self) -> None:
        sla = sla_summary(self.settings.sla_target_pct)
        if not sla["at_risk"]:
            return
        if not self._daily_alert_due(SLA_DAILY_KEY):
            return
        await self._post_sla_warning(sla)
        self._mark_daily_alert(SLA_DAILY_KEY)

    def _daily_alert_due(self, key: str) -> bool:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT last_at FROM daily_alert_state WHERE kind=?",
                (key,),
            ).fetchone()
        if not row:
            return True
        last = _parse_iso(row["last_at"])
        return (datetime.now(timezone.utc) - last) > timedelta(hours=24)

    def _mark_daily_alert(self, key: str) -> None:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO daily_alert_state(kind, last_at) VALUES (?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET last_at=excluded.last_at",
                (key, _to_iso(datetime.now(timezone.utc))),
            )

    def _record_alert(self, service_name: str, status: str, when: datetime) -> None:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO alert_state(service_name, last_alert_at, last_status) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(service_name) DO UPDATE SET "
                "  last_alert_at=excluded.last_alert_at, "
                "  last_status=excluded.last_status",
                (service_name, _to_iso(when), status),
            )

    async def _post_incident_alert(self, service_name: str, started: datetime, duration: timedelta) -> None:
        mins = int(duration.total_seconds() // 60)
        embed = {
            "title": f"🔴 {service_name} is down",
            "color": COLOR_RED,
            "description": f"Started <t:{int(started.timestamp())}:R> · ongoing for {mins} min",
            "url": self.settings.status_public_url,
            "footer": {"text": "YourBot status"},
        }
        if self.settings.brand_bot_avatar_url:
            embed["thumbnail"] = {"url": self.settings.brand_bot_avatar_url}
        await self._send({"embeds": [embed]})

    async def _post_resolved(self, service_name: str, duration_min: int) -> None:
        embed = {
            "title": f"🟢 {service_name} recovered",
            "color": COLOR_GREEN,
            "description": f"Resolved — duration {duration_min} min",
            "url": self.settings.status_public_url,
            "footer": {"text": "YourBot status"},
        }
        await self._send({"embeds": [embed]})

    async def _post_ssl_warning(self, days_left: int, tier: str) -> None:
        color = COLOR_RED if tier == "critical" else COLOR_AMBER
        embed = {
            "title": f"⚠ SSL certificate expires in {days_left} days",
            "color": color,
            "description": f"`{self.settings.probe_base_url}` — renew before expiry to avoid an outage.",
            "footer": {"text": "YourBot status"},
        }
        await self._send({"embeds": [embed]})

    async def _post_sla_warning(self, sla: dict) -> None:
        embed = {
            "title": "📉 SLA at risk",
            "color": COLOR_AMBER,
            "description": (
                f"Target: **{sla['target_pct']:.2f}%** · "
                f"Last {sla['days']} days: **{sla['actual_pct']:.3f}%**"
            ),
            "url": self.settings.status_public_url,
            "footer": {"text": "YourBot status"},
        }
        await self._send({"embeds": [embed]})

    async def _send(self, payload: dict) -> None:
        url = self.settings.alert_discord_webhook_url
        if not url:
            return
        try:
            r = await self._client.post(url, json=payload, timeout=5.0)
            if r.status_code >= 400:
                logger.warning("Discord webhook returned %s", r.status_code)
        except httpx.HTTPError as exc:
            logger.warning("Discord webhook send failed: %s", exc)


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
