from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import ingest
from ..config import get_settings
from ..ratelimit import limiter as _limiter

router = APIRouter(prefix="/ingest")


async def _signed_json(request: Request, secret: str, timestamp: str | None, signature: str | None) -> tuple[dict, int]:
    raw = await request.body()
    try:
        ts = ingest.verify(secret, timestamp, signature, raw)
        try:
            payload = json.loads(raw)
        except ValueError:
            raise ingest.Rejected(422, "body is not JSON") from None
        if not isinstance(payload, dict):
            raise ingest.Rejected(422, "body must be a JSON object")
    except ingest.Rejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    return payload, ts


@router.post("/platform", include_in_schema=False)
@_limiter.limit("30/minute")
async def platform_report(
    request: Request,
    x_status_timestamp: str | None = Header(default=None),
    x_status_signature: str | None = Header(default=None),
) -> JSONResponse:
    """The platform's own health report, sent by its checker once a minute:
    {"status": <what /status/api returns>, "shards": <what /status/api/shards returns>}."""
    payload, ts = await _signed_json(request, get_settings().ingest_platform_secret,
                                     x_status_timestamp, x_status_signature)
    try:
        ingest.store_platform_report(payload, ts)
    except ingest.Rejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    return JSONResponse({"ok": True})


@router.post("/vantage", include_in_schema=False)
@_limiter.limit("60/minute")
async def vantage_report(
    request: Request,
    x_status_timestamp: str | None = Header(default=None),
    x_status_signature: str | None = Header(default=None),
) -> JSONResponse:
    """One website check made from somewhere else:
    {"vantage": "cloudflare", "status": "operational" | "down", "http_status": 200,
     "response_ms": 180, "error": null, "label": "Cloudflare"}."""
    payload, ts = await _signed_json(request, get_settings().ingest_vantage_secret,
                                     x_status_timestamp, x_status_signature)
    try:
        name = ingest.store_vantage_report(payload, ts)
    except ingest.Rejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    return JSONResponse({"ok": True, "vantage": name})
