from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from . import db, ingest
from .aggregator import recent_proxy_services, roll_up_after_probe
from .alerter import Alerter
from .components import shard_name
from .config import Settings
from .probes import ProbeResult, live_test
from .probes.discord import probe_discord
from .probes.discord_status import probe_discord_status
from .probes.dns import probe_dns
from .probes.http import NOT_MEASURED_ERROR, derive_db_redis, probe_readiness
from .probes.monitor import monitor_online, parse_control_urls
from .probes.proxy import (
    PROXY_INTERNAL_SERVICES,
    probe_status_api,
    probe_status_shards,
    results_from_status_body,
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

# The platform's report about itself, by either road: fetched through the
# website ("proxy") or sent to us by its checker ("push").
PLATFORM_SOURCES = ("proxy", "push")

# The live test could not tell whose failure it saw: Discord itself was
# reporting trouble with its API or gateway at that moment.
DISCORD_TROUBLE_ERROR = "discord trouble"
# The live test has never been answered through this webhook: the setup is not
# finished, and its silence is not an outage yet.
NEVER_ANSWERED_ERROR = "never answered"
LIVE_TEST_ATTEMPTS = 2


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
        if new_rank > cur_rank or (new_rank == cur_rank and r.source in PLATFORM_SOURCES
                                   and cur.source not in PLATFORM_SOURCES):
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
        # The live test's webhook, checked once. A wrong address switches the
        # test off and says so, instead of posting somewhere else every minute.
        self._live_test_url = live_test.parse_webhook_url(settings.live_test_webhook_url)
        if settings.live_test_webhook_url.strip() and self._live_test_url is None:
            logger.error("LIVE_TEST_WEBHOOK_URL is not a Discord webhook address; the live test is off")

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

    @property
    def live_test_on(self) -> bool:
        return self._live_test_url is not None

    def _live_test_fresh_seconds(self) -> float:
        """How old the last finished test may be and still describe now: one
        interval, the longest a run can take, and some slack."""
        s = self.settings
        return max(30, s.live_test_interval_seconds) + LIVE_TEST_ATTEMPTS * (s.live_test_deadline_seconds + 20.0) + 30.0

    async def run_live_test_forever(self) -> None:
        """Post a test message, wait for the bot's reaction, remember the
        result. Its own loop, so a slow or failing test never delays the other
        checks: the cycle reads whatever finished last."""
        if not self.live_test_on:
            return
        settings = self.settings
        interval = max(30, settings.live_test_interval_seconds)
        logger.info("live test on: a test message every %ds, %.0fs for the bot to react",
                    interval, settings.live_test_deadline_seconds)
        while not self._stopping:
            started = time.perf_counter()
            try:
                result = await live_test.probe_live_test(
                    self._client, self._live_test_url,
                    emoji=settings.live_test_emoji,
                    deadline=settings.live_test_deadline_seconds,
                    slow_after=settings.live_test_slow_seconds,
                    attempts=LIVE_TEST_ATTEMPTS,
                )
                live_test.remember(result, self._live_test_url)
                if result.status in ("down", "unknown"):
                    logger.warning("live test: %s (%s)", result.status, result.error)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("live test raised; continuing")
            try:
                await asyncio.sleep(max(5.0, interval - (time.perf_counter() - started)))
            except asyncio.CancelledError:
                self._stopping = True
                raise

    def _live_test_result(self, monitor_offline: bool, discord_status: ProbeResult | None) -> ProbeResult | None:
        """This cycle's row for the live test, from the last finished run."""
        if not self.live_test_on:
            return None
        if monitor_offline:
            return _no_data(live_test.SERVICE_NAME, live_test.SOURCE, MONITOR_OFFLINE_ERROR)
        try:
            result = live_test.latest(self._live_test_fresh_seconds())
        except Exception:
            logger.exception("could not read the last live test")
            result = None
        if result is None:
            return _no_data(live_test.SERVICE_NAME, live_test.SOURCE, "no recent test")
        if result.status == "down" and discord_status is not None and discord_status.status in ("degraded", "down"):
            # The message may never have reached the bot. With Discord itself
            # in trouble we cannot tell, so this is not counted against us.
            return _no_data(live_test.SERVICE_NAME, live_test.SOURCE, DISCORD_TROUBLE_ERROR)
        if result.status == "down" and not self._live_test_answered_before():
            # Switched on, and the bot has not answered once yet: a permission
            # is missing in the test channel, or the platform was given the
            # wrong ids. That is a setup to finish, not an outage to publish.
            return _no_data(live_test.SERVICE_NAME, live_test.SOURCE, NEVER_ANSWERED_ERROR)
        return result

    def _live_test_answered_before(self) -> bool:
        try:
            return live_test.answered_before(self._live_test_url)
        except Exception:
            logger.exception("could not read whether the live test was ever answered")
            return True     # then believe the result, as before this rule existed

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

        # ── What did the other places see? ────────────────────────────────
        # Checks of the same website from unrelated networks, sent to us in
        # the last minute or so. Together with ours they decide whether the
        # site is down, so one bad route (ours included) is not an outage.
        vantage = self._vantage_reports()
        own_reached = readiness.status in ("operational", "degraded")   # did OUR request get an answer
        ingest.store_own_site_check(readiness.status, readiness.response_ms)
        readiness, vantage_note = site_consensus(readiness, vantage)
        if vantage_note:
            logger.info("website verdict from %d places: %s", len(vantage) + 1, vantage_note)

        # ── Is the failure ours? ──────────────────────────────────────────
        # Asked only when the website check failed every retry and nobody
        # else reported: a healthy cycle spends no requests on the control
        # endpoints. (With other places reporting, the question is already
        # answered: their reports reached us, so our connection works.)
        online: bool | None = None
        if readiness.status == "down" and not vantage:
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

        # ── What does the platform say about itself? ──────────────────────
        # Two roads to the same report. We fetch it through the website when
        # we can. Its checker also sends it to us directly, and that keeps
        # arriving when the website is down, or up but out of our reach.
        pushed = None if monitor_offline else self._pushed_platform_report()
        platform: list[ProbeResult] | None = None
        fetched: list[ProbeResult] | None = None
        shards: dict | None = None
        shard_source = "proxy"

        # Whether WE can read it depends on our own request, not on the
        # verdict: if most other places cannot reach the site but we can,
        # the site is down for most people and we can still look inside.
        if own_reached and not monitor_offline:
            fetched, status_body = await probe_status_api(
                self._client, settings.probe_base_url, expected_proxy,
            )
            shards = await probe_status_shards(self._client, settings.probe_base_url)
            if status_body is not None:
                platform = fetched

        if platform is None and pushed is not None:
            # Measurements made a moment ago, so the services behind the
            # website keep their real state instead of going to "no data".
            platform = results_from_status_body(pushed["status"], source="push")
            named = {r.service_name for r in platform}
            platform.extend(_no_data(name, "push", "not in the platform's report")
                            for name in expected_proxy if name not in named)
            if shards is None and pushed.get("shards"):
                shards, shard_source = pushed["shards"], "push"

        if platform is None and fetched is not None:
            platform = fetched      # the website answered but its status endpoint did not: "no data"
        if platform is None:
            # We cannot see inside the platform this cycle: the website is
            # confirmed unreachable (or only others can reach it, or our own
            # connection is down) and nothing was sent to us. The services
            # behind it were NOT measured, so they are "no data". They used
            # to be written `down` here, which copied every website blip onto
            # the bot, the gateway, the database and ten others.
            reason = MONITOR_OFFLINE_ERROR if monitor_offline else NOT_MEASURED_ERROR
            platform = [_no_data(name, "proxy", reason) for name in expected_proxy]
        results.extend(platform)

        if shards:
            self._store_shard_snapshot(shards)
            results.extend(_with_source(shard_results(shards), shard_source))
        else:
            # No fresh shard list by either road: stop presenting the old
            # snapshot as current.
            self._mark_shards_unreachable()

        results.append(dns)
        if ssl is not None:
            results.append(ssl)

        # ── Things that are not ours ──────────────────────────────────────
        discord_status: ProbeResult | None = None
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

        # ── Does a message in Discord still get an answer? ────────────────
        # Read after Discord's own status, because a missing reaction while
        # Discord is in trouble is not ours to count.
        live = self._live_test_result(monitor_offline, discord_status)
        if live is not None:
            results.append(live)

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

    def _vantage_reports(self) -> list[dict]:
        try:
            return ingest.vantage_reports()
        except Exception:
            logger.exception("could not read the vantage reports")
            return []

    def _pushed_platform_report(self) -> dict | None:
        try:
            return ingest.fresh_platform_report()
        except Exception:
            logger.exception("could not read the pushed platform report")
            return None

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


def site_consensus(local: ProbeResult, others: list[dict]) -> tuple[ProbeResult, str | None]:
    """The website's verdict from every place that checked it this minute.

    `local` is our own check (already retried). `others` are the fresh
    reports from other vantage points. The site counts as down only when
    MOST of the places that looked could not reach it. One place failing
    alone, ours or anyone's, is that place's route and not an outage.
    With nobody else reporting, our own check stands as it is (the caller
    then tests our own connection, as before).

    Returns the result to store and, when the places disagreed or a
    failure was confirmed, a short note saying how the vote went.
    """
    if not others or local.status not in ("operational", "degraded", "down"):
        return local, None
    failed = [o for o in others if o.get("status") == "down"]
    total = 1 + len(others)
    down_votes = len(failed) + (1 if local.status == "down" else 0)
    if down_votes == 0:
        return local, None
    if down_votes * 2 > total:
        note = f"unreachable from {down_votes} of {total} places"
        if local.status == "down":
            return local, note
        first = failed[0]
        return ProbeResult(
            service_name=local.service_name, status="down", source=local.source,
            http_status=first.get("http_status"), response_ms=None,
            error=(first.get("error") or note)[:200],
        ), note
    # A minority failed. The site is up for most; say so, and do not count it.
    note = f"reached from {total - down_votes} of {total} places"
    if local.status != "down":
        return local, note
    return ProbeResult(
        service_name=local.service_name, status="operational", source=local.source,
        response_ms=None, error=None,
        extra={"vantage_note": note + "; our own monitor could not connect"},
    ), note


def _with_source(results: list[ProbeResult], source: str) -> list[ProbeResult]:
    for r in results:
        r.source = source
    return results


_SHARD_STATES = ("operational", "degraded", "down")


def shard_results(shards: dict) -> list[ProbeResult]:
    """One check per shard, once the shared bot runs on more than one.

    The platform's Gateway check passes while ANY shard is alive. With
    several shards that is no longer the whole bot: one dead shard means
    every server on it has no bot, and the page must say so. With a single
    shard the Gateway check already is that shard, so nothing is added.
    """
    worst: dict[int, ProbeResult] = {}
    for cluster in shards.get("clusters") or []:
        for shard in cluster.get("shards") or []:
            try:
                shard_id = int(shard.get("shard_id"))
            except (TypeError, ValueError):
                continue
            status = shard.get("status")
            if status not in _SHARD_STATES:
                status = "unknown"
            latency = shard.get("latency_ms")
            result = ProbeResult(
                service_name=shard_name(shard_id), status=status, source="proxy",
                response_ms=int(latency) if isinstance(latency, (int, float)) and status != "down" else None,
                error=None if status != "unknown" else "no report for this shard",
            )
            seen = worst.get(shard_id)
            if seen is None or _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(seen.status, 0):
                worst[shard_id] = result
    if len(worst) < 2:
        return []
    return [worst[shard_id] for shard_id in sorted(worst)]


def _no_data(service_name: str, source: str, reason: str) -> ProbeResult:
    return ProbeResult(service_name=service_name, status="unknown", error=reason, source=source)
