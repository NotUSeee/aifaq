from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from . import db
from .aggregator import roll_up_after_probe
from .alerter import Alerter
from .config import Settings
from .probes import ProbeResult
from .probes.discord import probe_discord
from .probes.dns import probe_dns
from .probes.http import derive_db_redis, probe_health, probe_readiness
from .probes.proxy import PROXY_INTERNAL_SERVICES, probe_status_api, probe_status_shards
from .probes.ssl import probe_ssl

logger = logging.getLogger("status_service.scheduler")

SSL_PROBE_INTERVAL_SECONDS = 3600  # 1 hour


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
        self._last_ssl_check = 0.0

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

    async def _cycle(self) -> None:
        results: list[ProbeResult] = []

        readiness, body = await probe_readiness(self._client, self.settings.probe_base_url)
        results.append(readiness)
        results.extend(derive_db_redis(readiness, body))

        if readiness.status != "down":
            proxy_results, _ = await probe_status_api(self._client, self.settings.probe_base_url)
            results.extend(proxy_results)

            shards = await probe_status_shards(self._client, self.settings.probe_base_url)
            if shards:
                self._store_shard_snapshot(shards)
        else:
            # Public site unreachable → can't reach /status/api either. Emit
            # `unknown` for every internal service so the page flips off stale
            # `operational` rows immediately, rather than waiting 5 min for
            # the aggregator's stale-out window.
            for name in PROXY_INTERNAL_SERVICES:
                results.append(ProbeResult(
                    service_name=name,
                    status="unknown",
                    error="public site unreachable",
                    source="proxy",
                ))

        results.append(await probe_dns(self.settings.probe_base_url))

        now_perf = time.perf_counter()
        if now_perf - self._last_ssl_check > SSL_PROBE_INTERVAL_SECONDS:
            results.append(await probe_ssl(
                self.settings.probe_base_url,
                warn_days=self.settings.ssl_warn_days,
                critical_days=self.settings.ssl_critical_days,
            ))
            self._last_ssl_check = now_perf

        discord_result = await probe_discord(self._client, self.settings.discord_bot_token)
        if discord_result is not None:
            results.append(discord_result)

        self._persist(results)
        roll_up_after_probe()

        try:
            await self._alerter.evaluate(results)
        except Exception:
            logger.exception("alerter raised")

        await self._heartbeat()

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

    def _store_shard_snapshot(self, shards: dict) -> None:
        rows = []
        clusters = shards.get("clusters") or []
        for cluster_idx, cluster in enumerate(clusters):
            for shard in cluster.get("shards") or []:
                rows.append((
                    cluster_idx,
                    int(shard.get("shard_id", 0)),
                    shard.get("status", "unknown"),
                    shard.get("latency_ms"),
                    shard.get("guild_count"),
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
