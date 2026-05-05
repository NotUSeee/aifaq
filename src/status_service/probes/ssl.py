from __future__ import annotations

import asyncio
import socket
import ssl as _ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import ProbeResult


def _parse_not_after(cert: dict) -> datetime | None:
    """openssl-style cert dicts use 'notAfter' formatted like
    'Jun  1 12:00:00 2026 GMT'."""
    raw = cert.get("notAfter")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _probe_blocking(host: str, port: int, timeout: float) -> tuple[dict | None, str | None]:
    ctx = _ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as sock:
            return sock.getpeercert(), None


async def probe_ssl(base_url: str, warn_days: int, critical_days: int, timeout: float = 5.0) -> ProbeResult:
    """Verify the TLS certificate of base_url and surface days-to-expiry.

    Status:
      - operational: cert valid, > warn_days remaining
      - degraded:    cert valid, between critical_days and warn_days
      - down:        cert valid, < critical_days OR handshake failed
    """
    parsed = urlparse(base_url)
    host = parsed.hostname or base_url
    port = parsed.port or 443
    started = time.perf_counter()
    try:
        cert, _ = await asyncio.wait_for(
            asyncio.to_thread(_probe_blocking, host, port, timeout),
            timeout=timeout + 1.0,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
    except (asyncio.TimeoutError, socket.timeout):
        return ProbeResult(
            service_name="SSL Certificate",
            status="down",
            response_ms=int(timeout * 1000),
            error="tls handshake timeout",
            source="ssl",
        )
    except (_ssl.SSLError, OSError) as exc:
        return ProbeResult(
            service_name="SSL Certificate",
            status="down",
            response_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc)[:200],
            source="ssl",
        )

    not_after = _parse_not_after(cert or {})
    if not_after is None:
        return ProbeResult(
            service_name="SSL Certificate",
            status="unknown",
            response_ms=elapsed_ms,
            error="no notAfter in cert",
            source="ssl",
        )

    days_left = (not_after - datetime.now(timezone.utc)).days
    if days_left < critical_days:
        status = "down"
    elif days_left < warn_days:
        status = "degraded"
    else:
        status = "operational"

    return ProbeResult(
        service_name="SSL Certificate",
        status=status,
        response_ms=elapsed_ms,
        source="ssl",
        extra={"days_left": days_left, "not_after": not_after.isoformat()},
        error=None if status == "operational" else f"{days_left} days to expiry",
    )
