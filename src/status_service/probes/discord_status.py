"""Discord's own status, read from Discord's public status page.

When a bot stops answering, the first question is "is it Discord or is it
YourBot?". This probe answers the Discord half from the source, with no
token: discordstatus.com is a standard Statuspage site with a JSON summary.

We only relay what Discord says. A failure to fetch the summary is recorded
as "unknown", never as Discord being down.
"""

from __future__ import annotations

import time

import httpx

from . import USER_AGENT, ProbeResult

SERVICE_NAME = "Discord"

# The Discord components a bot depends on. Voice servers, payments or the
# marketing site having trouble does not stop a bot from working.
RELEVANT_COMPONENTS = ("API", "Gateway")

_COMPONENT_STATUS = {
    "operational": "operational",
    "degraded_performance": "degraded",
    "partial_outage": "degraded",
    "under_maintenance": "degraded",
    "major_outage": "down",
}
_RANK = {"operational": 0, "degraded": 1, "down": 2}
_PHRASE = {
    "degraded_performance": "slow",
    "partial_outage": "partly unavailable",
    "under_maintenance": "under maintenance",
    "major_outage": "unavailable",
}


def summarize(body: dict) -> tuple[str, str | None]:
    """Reduce a Statuspage summary to (status, note). `note` is a short
    public sentence when something is wrong, else None."""
    components = body.get("components") if isinstance(body, dict) else None
    worst = "operational"
    problems: list[str] = []
    seen = False
    for comp in components or []:
        if not isinstance(comp, dict) or comp.get("name") not in RELEVANT_COMPONENTS:
            continue
        seen = True
        raw = str(comp.get("status") or "")
        mapped = _COMPONENT_STATUS.get(raw)
        if mapped is None:
            continue
        if _RANK[mapped] > _RANK[worst]:
            worst = mapped
        if mapped != "operational":
            problems.append(f"Discord's {comp['name']} is {_PHRASE.get(raw, 'having problems')}")
    if not seen:
        return "unknown", "Discord's status page did not list its API or gateway"
    if worst == "operational":
        return "operational", None

    note = " and ".join(problems)
    incidents = body.get("incidents") if isinstance(body, dict) else None
    for inc in incidents or []:
        if isinstance(inc, dict) and inc.get("name"):
            note += f". Discord says: {str(inc['name'])[:140]}"
            break
    return worst, note[:300]


async def probe_discord_status(client: httpx.AsyncClient, summary_url: str) -> ProbeResult | None:
    """Fetch Discord's status summary. Returns None when disabled (blank URL)."""
    if not summary_url:
        return None
    started = time.perf_counter()
    try:
        r = await client.get(summary_url, headers={"User-Agent": USER_AGENT})
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if r.status_code != 200:
            return ProbeResult(
                service_name=SERVICE_NAME, status="unknown", response_ms=elapsed_ms,
                http_status=r.status_code, error="could not read Discord's status page",
                source="discord_status",
            )
        status, note = summarize(r.json())
    except (httpx.HTTPError, ValueError):
        return ProbeResult(
            service_name=SERVICE_NAME, status="unknown",
            error="could not read Discord's status page", source="discord_status",
        )
    return ProbeResult(
        service_name=SERVICE_NAME, status=status, response_ms=elapsed_ms,
        http_status=200, error=note, source="discord_status",
    )
