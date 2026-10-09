from __future__ import annotations

import os
import tempfile

import pytest

from status_service import db
from status_service.config import reset_settings
from status_service.ratelimit import limiter
from status_service.routes.ui import clear_live_cache
from status_service.snapshot import clear_cache


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Each test gets a fresh SQLite file, a clean Settings instance, and
    empty rate-limit buckets (the shared limiter's in-memory counts would
    otherwise accumulate across tests — e.g. the 10/minute login limit
    trips partway through a full-suite run). Keeps the suite hermetic."""
    limiter.reset()
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)
    monkeypatch.setenv("PROBE_BASE_URL", "https://test.example.com")
    monkeypatch.setenv("PROBE_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("ADMIN_HMAC_SECRET", "test-secret-deadbeef")
    monkeypatch.setenv("ALERT_DISCORD_WEBHOOK_URL", "")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
    # No snapshot cache in tests: each assertion must see the rows just written.
    monkeypatch.setenv("API_CACHE_SECONDS", "0")
    reset_settings()
    clear_cache()
    clear_live_cache()
    db.init_db()
    yield
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    reset_settings()


@pytest.fixture
def fresh_db():
    """Convenience handle for tests that want to insert directly."""
    with db.connect() as conn:
        yield conn
