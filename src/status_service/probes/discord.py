from __future__ import annotations

import time

import httpx

from . import USER_AGENT, ProbeResult

DISCORD_API = "https://discord.com/api/v10"


async def probe_discord(client: httpx.AsyncClient, bot_token: str) -> ProbeResult | None:
    """Hit Discord's /users/@me with the bot token to confirm Discord's API
    is reachable and can authenticate the bot. Returns None if no token is
    configured. `bot_token` should be a token with `identify` scope only.

    Reports as its own "Discord API" component — NOT "Bot". The platform's
    /status/api already owns the "Bot" component; when this probe shared
    that name its (higher-id) row won in latest_per_service(), so a healthy
    Discord API masked a real Bot outage during full-site downtime."""
    if not bot_token:
        return None
    started = time.perf_counter()
    try:
        r = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={
                "Authorization": f"Bot {bot_token}",
                "User-Agent": USER_AGENT,
            },
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if r.status_code == 200:
            return ProbeResult(
                service_name="Discord API",
                status="operational" if elapsed_ms < 2000 else "degraded",
                response_ms=elapsed_ms,
                http_status=200,
                source="discord",
            )
        if r.status_code == 401:
            return ProbeResult(
                service_name="Discord API",
                status="down",
                response_ms=elapsed_ms,
                http_status=401,
                error="discord auth failed",
                source="discord",
            )
        return ProbeResult(
            service_name="Discord API",
            status="degraded",
            response_ms=elapsed_ms,
            http_status=r.status_code,
            error=f"discord returned {r.status_code}",
            source="discord",
        )
    except httpx.TimeoutException:
        return ProbeResult(
            service_name="Discord API",
            status="down",
            error="discord api timeout",
            source="discord",
        )
    except httpx.HTTPError as exc:
        return ProbeResult(
            service_name="Discord API",
            status="down",
            error=str(exc)[:200],
            source="discord",
        )
