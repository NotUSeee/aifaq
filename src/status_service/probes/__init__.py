from __future__ import annotations

from dataclasses import dataclass

USER_AGENT = "yourbot-status/1.0 (+https://status.yourbot.work)"


@dataclass
class ProbeResult:
    """Single probe outcome. `service_name` is the canonical service this
    probe maps to (e.g., "Public Site"). `source` distinguishes the kind
    of probe that produced it ("external", "proxy", "discord", "dns",
    "ssl") so multiple probes can vote on the same service."""

    service_name: str
    status: str  # operational | degraded | down | unknown
    response_ms: int | None = None
    http_status: int | None = None
    error: str | None = None
    source: str = "external"
    extra: dict | None = None
