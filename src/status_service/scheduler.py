from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from . import db
from .aggregator import recent_proxy_services, roll_up_after_probe
from .alerter import Alerter
from .config import Settings
from .probes import ProbeResult
from .probes.discord import probe_discord
from .probes.discord_status import probe_discord_status
from .probes.dns import probe_dns
from .probes.http import NOT_MEASURED_ERROR, derive_db_redis, probe_readiness
from .probes.monitor import monitor_online, parse_control_urls
from .probes.proxy import (
    PROXY_INTERNAL_SERVICES,
    probe_status_api,
    probe_status_shards,
)
from .probes.ssl import probe_ssl

logger = logging.getLogger("status_service.scheduler")

SSL_PROBE_INTERVAL_SECONDS = 3600  # 1 hour
PROBE_RETENTION_DAYS = 30  # prune probe_results past this, once per UTC day

# Service name for the monitor's own heartbeat row. The "__" prefix keeps it
# out of every public list, uptime figure and incident.
MONITOR_SERVICE = "__monitor__"
MONITOR_OFFLINE_ERROR = "monitor offline"

_STATUS_RANK = {"unknown": 0, "operational": 1, "degraded": 2, "down": 3}


def merge_cycle_results(results: list[ProbeResult]) -> list[ProbeResult]:
    """One row per service per cycle.

    Database and Cache are reported twice: by the website's /readiness
    body and by the platform's own checker. Storing both doubled their
    check counts and made the response-time chart flip between two
    unrelated timings. Keep the worse verdict of the two (a problem either
    one saw is real); on a tie prefer the platform checker's row, which
    carries that service's own timing.
    """
    best: dict[str, ProbeResult] = {}
    order: list[str] = []
    for r in results:
        cur = best.get(r.service_name)
        if cur is None:
            best[r.service_name] = r
            order.append(r.service_name)
            continue
        new_rank, cur_rank = _STATUS_RANK.get(r.status, 0), _STATUS_RANK.get(cur.status, 0)
        if new_rank > cur_rank or (new_rank == cur_rank and r.source == "proxy" and cur.source != "proxy"):
            best[r.service_name] = r
    return [best[name] for name in order]


class Scheduler:
    """Drives the probe loop. One instance per process. SIGTERM/SIGINT
    propagate via the FastAPI lifespan, which calls stop()/aclose() to
    shut down cleanly without dropping in-flight probes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stopping = False
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0),
            follow_redirects=True,
        )
        self._alerter = Alerter(settings, self._client)
        self._last_ssl_check: float | None = None
        self._last_prune_day: str | None = None

    def stop(self) -> None:
        self._stopping = True

    async def aclose(self) -> None:
        await self._client.aclose()

    async def run_forever(self) -> None:
        """Probe → store → roll up → alert → heartbeat. Repeats every
        PROBE_INTERVAL_SECONDS, accounting for elapsed time so cadence
        is steady even if a cycle runs long."""
        while not self._stopping:
            cycle_started = time.perf_counter()
            try:
                await self._cycle()
            except Exception:
                logger.exception("probe cycle raised; continuing")
            elapsed = time.perf_counter() - cycle_started
            sleep_for = max(1.0, self.settings.probe_interval_seconds - elapsed)
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                self._stopping = True
                raise

    def _expected_proxy_services(self) -> list[str]:
        """Every service the platform reports: the core list, plus whatever
        else it has been reporting lately (optional stack members, the dev
        portal, future services). When the platform cannot be read they all
        go to "no data" in the same cycle, so none is left showing its last
        "operational" into an outage."""
        expected = list(PROXY_INTERNAL_SERVICES)
        try:
            recent = recent_proxy_services()
        except Exception:
            logger.exception("could not read the list of reported services")
            recent = set()
        # Database and Cache are handled through the /readiness reading.
        expected.extend(sorted(s for s in recent if s not in expected and s not in ("Database", "Cache")))
        return expected

    async def _cycle(self) -> None:
        settings = self.settings
        results: list[ProbeResult] = []
        expected_proxy = self._expected_proxy_services()

        # ── Outside checks ────────────────────────────────────────────────
        readiness, body = await probe_readiness(
            self._client, settings.probe_base_url,
            attempts=settings.probe_attempts,
            retry_delay=settings.probe_retry_delay_seconds,
        )
        dns = await probe_dns(settings.probe_base_url, attempts=2)

        ssl: ProbeResult | None = None
        now_perf = time.perf_counter()
        ssl_due = self._last_ssl_check is None or now_perf - self._last_ssl_check > SSL_PROBE_INTERVAL_SECONDS
        # Only an https site has a certificate to check.
        if ssl_due and settings.probe_base_url.lower().startswith("https://"):
            ssl = await probe_ssl(
                settings.probe_base_url,
                warn_days=settings.ssl_warn_days,
                critical_days=settings.ssl_critical_days,
            )
            # A check that could not connect is not a result: look again next
            # cycle instead of leaving "no data" on the page for an hour.
            self._last_ssl_check = None if ssl.status == "unknown" else now_perf

        # ── Is the failure ours? ──────────────────────────────────────────
        # Asked only when the website check failed every retry: a healthy
        # cycle spends no requests on the control endpoints.
        online: bool | None = None
        if readiness.status == "down":
            online = await monitor_online(self._client, parse_control_urls(settings.monitor_control_urls))
        monitor_offline = online is False

        if monitor_offline:
            # Our own connection is down. Nothing was measured this cycle, so
            # nothing is recorded as downtime: every row is "no data".
            logger.warning("monitor offline: no control endpoint reachable; recording this cycle as no data")
            readiness = _no_data(readiness.service_name, readiness.source, MONITOR_OFFLINE_ERROR)
            dns = _no_data(dns.service_name, dns.source, MONITOR_OFFLINE_ERROR)
            body = {}
        elif dns.status == "down" and readiness.status != "down":
            # The site just answered by name, so the domain resolves. A lookup
            # that failed anyway is our resolver, not a broken domain.
            dns = _no_data(dns.service_name, dns.source, "lookup failed while the site answered")

        results.append(readiness)
        results.extend(derive_db_redis(readiness, body))

        if readiness.status in ("down", "unknown"):
            # We cannot see inside the platform this cycle: either the website
            # is confirmed unreachable, or our own connection is down. The
            # services behind it were NOT measured, so they are "no data".
            # They used to be written `down` here, which copied every website
            # blip onto the bot, the gateway, the database and ten others.
            reason = MONITOR_OFFLINE_ERROR if monitor_offline else NOT_MEASURED_ERROR
            for name in expected_proxy:
                results.append(_no_data(name, "proxy", reason))
            self._mark_shards_unreachable()
        else:
            proxy_results, _ = await probe_status_api(
                self._client, settings.probe_base_url, expected_proxy,
            )
            results.extend(proxy_results)

            shards = await probe_status_shards(self._client, settings.probe_base_url)
            if shards:
                self._store_shard_snapshot(shards)
            else:
                # /status/api/shards failed but readiness was OK — fresh shard
                # data is unavailable, so stop presenting the old snapshot as
                # current.
                self._mark_shards_unreachable()

        results.append(dns)
        if ssl is not None:
            results.append(ssl)

        # ── Things that are not ours ──────────────────────────────────────
        if monitor_offline:
            if settings.discord_status_url:
                results.append(_no_data("Discord", "discord_status", MONITOR_OFFLINE_ERROR))
            if settings.discord_bot_token:
                results.append(_no_data("Discord API", "discord", MONITOR_OFFLINE_ERROR))
        else:
            discord_status = await probe_discord_status(self._client, settings.discord_status_url)
            if discord_status is not None:
                results.append(discord_status)
            discord_result = await probe_discord(self._client, settings.discord_bot_token)
            if discord_result is not None:
                results.append(discord_result)

        results = merge_cycle_results(results)
        results.append(ProbeResult(
            service_name=MONITOR_SERVICE,
            status="down" if monitor_offline else "operational",
            error=MONITOR_OFFLINE_ERROR if monitor_offline else None,
            source="monitor",
        ))

        self._persist(results)
        roll_up_after_probe()

        try:
            db.expire_ended_maintenance()
        except Exception:
            logger.exception("maintenance expiry raised")

        self._prune_if_due()

        try:
            await self._alerter.evaluate(results)
        except Exception:
            logger.exception("alerter raised")

        if not monitor_offline:
            await self._heartbeat()

    def _prune_if_due(self) -> None:
        """Once per UTC day, drop probe_results beyond the retention window
        and VACUUM. Without this the SQLite file grows unbounded (~20 rows
        per minute, forever)."""
        today = datetime.now(timezone.utc).date().isoformat()
        if self._last_prune_day == today:
            return
        self._last_prune_day = today
        try:
            pruned = db.prune_old_probes(PROBE_RETENTION_DAYS)
            if pruned:
                db.vacuum()
            logger.info("retention prune: %d probe rows removed", pruned)
        except Exception:
            logger.exception("retention prune failed")

    def _persist(self, results: list[ProbeResult]) -> None:
        if not results:
            return
        rows = [
            (r.service_name, r.status, r.response_ms, r.http_status, r.error, r.source)
            for r in results
        ]
        if not rows:
            return
        with db.connect() as conn:
            conn.executemany(
                "INSERT INTO probe_results(service_name,status,response_ms,http_status,error,source) "
                "VALUES (?,?,?,?,?,?)",
                rows,
            )

    def _mark_shards_unreachable(self) -> None:
        """Flip every row in shard_snapshot to status='unknown' so the page
        stops presenting a stale 'operational' snapshot as current when
        /status/api/shards cannot be read. Not 'down': failing to read the
        shard list says nothing about whether the shards are connected.
        Server counts and shard ids are preserved."""
        with db.connect() as conn:
            conn.execute("UPDATE shard_snapshot SET status='unknown'")

    def _store_shard_snapshot(self, shards: dict) -> None:
        rows = []
        clusters = shards.get("clusters") or []
        for cluster_idx, cluster in enumerate(clusters):
            for shard in cluster.get("shards") or []:
                # Platform sends `guilds` per-shard (see api_status.status_shards),
                # but tolerate `guild_count` too in case the schema flips back.
                guilds = shard.get("guilds")
                if guilds is None:
                    guilds = shard.get("guild_count")
                rows.append((
                    cluster_idx,
                    int(shard.get("shard_id", 0)),
                    shard.get("status", "unknown"),
                    shard.get("latency_ms"),
                    guilds,
                    datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                ))
        if not rows:
            return
        with db.connect() as conn:
            conn.execute("DELETE FROM shard_snapshot")
            conn.executemany(
                "INSERT INTO shard_snapshot(cluster_idx,shard_id,status,latency_ms,guild_count,fetched_at) "
                "VALUES (?,?,?,?,?,?)",
                rows,
            )

    async def _heartbeat(self) -> None:
        url = self.settings.heartbeat_ping_url
        if not url:
            return
        try:
            await self._client.get(url, timeout=5.0)
        except httpx.HTTPError:
            logger.warning("heartbeat ping failed", exc_info=False)


def _no_data(service_name: str, source: str, reason: str) -> ProbeResult:
    return ProbeResult(service_name=service_name, status="unknown", error=reason, source=source)
