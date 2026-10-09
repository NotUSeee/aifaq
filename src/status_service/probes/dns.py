from __future__ import annotations

import asyncio
import socket
import time
from urllib.parse import urlparse

from . import ProbeResult


async def _resolve_once(host: str, timeout: float) -> tuple[bool, int, str | None]:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM),
            timeout=timeout,
        )
        return True, int((time.perf_counter() - started) * 1000), None
    except asyncio.TimeoutError:
        return False, int(timeout * 1000), "resolve timeout"
    except socket.gaierror as exc:
        return False, int((time.perf_counter() - started) * 1000), str(exc)[:200]


async def probe_dns(base_url: str, timeout: float = 3.0, attempts: int = 1, retry_delay: float = 1.0) -> ProbeResult:
    """Resolve the hostname of base_url.

    This uses the monitor's own resolver, so a failure can just as easily
    be that resolver hiccuping as the domain being broken. A failure is
    retried, and the scheduler additionally checks that the monitor is
    online before reporting it as downtime.
    """
    host = urlparse(base_url).hostname or base_url
    attempts = max(1, int(attempts))
    ok, elapsed_ms, error = await _resolve_once(host, timeout)
    tries = 1
    while not ok and tries < attempts:
        await asyncio.sleep(max(0.0, retry_delay))
        ok, elapsed_ms, error = await _resolve_once(host, timeout)
        tries += 1
    if ok:
        return ProbeResult(
            service_name="DNS",
            status="operational" if elapsed_ms < 500 else "degraded",
            response_ms=elapsed_ms,
            source="dns",
        )
    return ProbeResult(
        service_name="DNS",
        status="down",
        response_ms=elapsed_ms,
        error=error,
        source="dns",
    )
