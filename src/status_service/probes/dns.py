from __future__ import annotations

import asyncio
import socket
import time
from urllib.parse import urlparse

from . import ProbeResult


async def probe_dns(base_url: str, timeout: float = 3.0) -> ProbeResult:
    """Resolve the hostname of base_url. A failure here usually means
    Cloudflare DNS is down for the zone or the local resolver is broken."""
    host = urlparse(base_url).hostname or base_url
    started = time.perf_counter()
    try:
        await asyncio.wait_for(
            asyncio.get_event_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM),
            timeout=timeout,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(
            service_name="DNS",
            status="operational" if elapsed_ms < 500 else "degraded",
            response_ms=elapsed_ms,
            source="dns",
        )
    except asyncio.TimeoutError:
        return ProbeResult(
            service_name="DNS",
            status="down",
            response_ms=int(timeout * 1000),
            error="resolve timeout",
            source="dns",
        )
    except socket.gaierror as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(
            service_name="DNS",
            status="down",
            response_ms=elapsed_ms,
            error=str(exc)[:200],
            source="dns",
        )
