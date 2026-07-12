from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .aggregator import sla_summary
from .config import Settings
from .probes import ProbeResult

logger = logging.getLogger("status_service.alerter")

COLOR_RED = 0xE05A5A
COLOR_GREEN = 0x6BCB8B
COLOR_AMBER = 0xE0A33E

SLA_DAILY_KEY = "sla_at_risk"
SSL_WARN_KEY_PREFIX = "ssl_warn_"


class Alerter:
    """Decides when to fire Discord webhook alerts based on probe results
    and aggregator state. Suppresses flapping by waiting ALERT_THRESHOLD_MIN
    before firing and enforcing ALERT_COOLDOWN_MIN between alerts per service."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.settings.alert_discord_webhook_url)

    async def evaluate(self, results: list[ProbeResult]) -> None:
        if not self.enabled:
            return
        await self._evaluate_incidents()
        await self._evaluate_ssl(results)
        await self._evaluate_sla()

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
