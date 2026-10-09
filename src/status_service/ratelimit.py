"""Single shared slowapi Limiter, keyed by the real visitor.

Every route module used to construct its own ``Limiter`` instance, each
with separate in-memory storage, while ``main.py`` registered yet another
one on ``app.state``. Enforcement still worked (the decorating instance
does the counting), but limits were tracked per-module and the app-level
default was inert. One shared instance keeps all buckets in one place.

WHO IS THE VISITOR. This service sits behind a Cloudflare Tunnel, so every
request reaches it from the same address (cloudflared, via the Docker
bridge). Keyed on that address, all visitors shared ONE bucket: sixty page
loads a minute for the whole internet, and a "Rate limit exceeded" error
for everyone past that, which is to say during any incident worth opening
a status page for. Verified on the live page 2026-10-08: thirty requests
from one network used up /api/incidents for a client on another.

The limiter therefore keys on the visitor address Cloudflare supplies in
``CF-Connecting-IP``. That header can be trusted here because the port is
only published on the host's loopback (docker-compose.yml), so nothing
reaches the app except through the tunnel, and Cloudflare overwrites any
value a visitor sends. Set ``CLIENT_IP_HEADER`` to another header for a
different proxy, or blank it to key on the socket address when the service
is exposed directly.
"""

from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request


def client_key(request: Request) -> str:
    header = os.environ.get("CLIENT_IP_HEADER", "CF-Connecting-IP").strip()
    if header:
        value = (request.headers.get(header) or "").strip()
        if value:
            return value[:64]
    return get_remote_address(request)


limiter = Limiter(key_func=client_key)
