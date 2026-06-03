from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import __version__, db
from .config import get_settings
from .scheduler import Scheduler

logger = logging.getLogger("status_service")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

limiter = Limiter(key_func=get_remote_address, default_limits=["600/minute"])

_HERE = Path(__file__).parent
_STATIC_DIR = _HERE / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    settings = get_settings()
    scheduler = Scheduler(settings)
    app.state.scheduler = scheduler
    task = asyncio.create_task(scheduler.run_forever(), name="status-scheduler")
    logger.info("status_service v%s started; probing %s every %ds",
                __version__, settings.probe_base_url, settings.probe_interval_seconds)
    try:
        yield
    finally:
        scheduler.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await scheduler.aclose()
        logger.info("status_service shutting down")


app = FastAPI(
    title="YourBot Status",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.middleware("http")
async def no_cache_headers(request: Request, call_next):
    """Forbid Cloudflare and browser caching of status responses so
    visitors always see live data. Static assets get their own caching
    via the /static mount + immutable filenames."""
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store, max-age=0")
        response.headers.setdefault("Vary", "Accept")
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/health")
@limiter.limit("600/minute")
async def health(request: Request) -> JSONResponse:
    """Lightweight liveness check used by Docker healthcheck."""
    return JSONResponse({"ok": True, "service": "status", "version": __version__})


from .routes import api as _api_routes  # noqa: E402
from .routes import ui as _ui_routes  # noqa: E402
from .routes import badge as _badge_routes  # noqa: E402
from .routes import admin as _admin_routes  # noqa: E402
from .routes import admin_ui as _admin_ui_routes  # noqa: E402

app.include_router(_ui_routes.router)
app.include_router(_api_routes.router)
app.include_router(_badge_routes.router)
app.include_router(_admin_routes.router)
app.include_router(_admin_ui_routes.router)
