"""Single shared slowapi Limiter.

Every route module used to construct its own ``Limiter`` instance, each
with separate in-memory storage, while ``main.py`` registered yet another
one on ``app.state``. Enforcement still worked (the decorating instance
does the counting), but limits were tracked per-module and the app-level
default was inert. One shared instance keeps all buckets in one place.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
