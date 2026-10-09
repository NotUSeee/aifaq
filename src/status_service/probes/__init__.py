from __future__ import annotations

import os
from dataclasses import dataclass

# How the prober identifies itself to the platform. Overridable so a second
# instance (a local preview, a staging copy) can be told apart from the real
# one in the platform's request logs. Keep the "yourbot-status/" prefix: the
# platform's firewall rule for the prober matches on it.
USER_AGENT = os.environ.get("PROBE_USER_AGENT") or "yourbot-status/1.0 (+https://status.yourbot.work)"


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
