"""Is the monitor itself online?

This service runs on one machine with one internet connection. When that
connection drops, every outbound probe fails, and without this check the
page would blame YourBot for it: on 2026-10-02 and 2026-09-21 it published
a three-minute outage of fourteen components while production was serving
every other visitor normally. The probes simply never left the building.

So before a failed probe is believed, the monitor checks that it can reach
the internet at all, using a couple of well-known endpoints run by
unrelated companies. If none of them answer, the fault is ours and the
cycle is recorded as "no data" instead of as downtime.
"""

from __future__ import annotations

import asyncio

import httpx

from . import USER_AGENT

# Two independent networks. A site-wide Google problem could take the first
# one down together with yourbot.gg (which sits behind a Google load
# balancer), so it must never be the only control.
DEFAULT_CONTROL_URLS = (
    "https://www.gstatic.com/generate_204",
    "https://cloudflare.com/cdn-cgi/trace",
)

_CONTROL_TIMEOUT = httpx.Timeout(connect=3.0, read=4.0, write=4.0, pool=4.0)


def parse_control_urls(raw: str) -> list[str]:
    return [u.strip() for u in (raw or "").split(",") if u.strip()]


async def _reachable(client: httpx.AsyncClient, url: str) -> bool:
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=_CONTROL_TIMEOUT)
    except (httpx.HTTPError, OSError):
        return False
    # Any HTTP answer proves the connection works; we are not grading the
    # control site, only our own path to the internet.
    return r.status_code < 500


async def monitor_online(client: httpx.AsyncClient, control_urls: list[str]) -> bool | None:
    """True if ANY control endpoint answers, False if none do.

    Returns None when no control endpoints are configured: the caller then
    has no way to tell and must fall back to believing its probes.
    """
    if not control_urls:
        return None
    results = await asyncio.gather(*(_reachable(client, u) for u in control_urls))
    return any(results)
