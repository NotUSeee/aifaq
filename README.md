# status_service — external status page for MMO Maid

`status.mmomaid.work` runs on the user's Ubuntu home server, alongside
the FAQ AI, exposed via the same Cloudflare Tunnel. It probes
`mmomaid.cloud` from outside every 60s, stores history in SQLite, and
serves the public-facing status site.

When `mmomaid.cloud` is up, the prober proxies through to
`/status/api*` for internal-tier service data (DB, Redis, workers, bot
shards). When `mmomaid.cloud` is down, the page itself stays online —
because it's on a different machine — and records the full outage.

## Run locally for development

```bash
python -m venv .venv
source .venv/bin/activate           # on Windows: .venv\Scripts\activate
pip install -e .[dev]

# Point at staging or your dev MMO Maid instance
export PROBE_BASE_URL=https://mmomaid.cloud
export DB_PATH=./data/status.db
export ADMIN_HMAC_SECRET="$(openssl rand -hex 32)"

uvicorn status_service.main:app --reload --port 8081
# Open http://127.0.0.1:8081
```

## Run tests

```bash
pytest
```

35 tests covering probes, aggregator, alerter, badge, admin auth, API
contract.

## Deploy

See [DEPLOY.md](./DEPLOY.md) for the Ubuntu installer runbook.

## Architecture

```
visitors ─HTTPS─► Cloudflare Tunnel ─► cloudflared ─► status_service (:8081)
                                                       │
                                                       ├─ probe loop (60s)
                                                       │   ├─ HTTPS  /health, /readiness
                                                       │   ├─ PROXY  /status/api, /status/api/shards
                                                       │   ├─ DNS    resolve mmomaid.cloud
                                                       │   ├─ SSL    cert expiry
                                                       │   └─ DISCORD bot identity (optional)
                                                       ├─ alerter   (Discord webhook, SLA, SSL)
                                                       └─ SQLite (probe_results, incidents, daily_uptime,
                                                                  shard_snapshot, announcements, alert_state)
                outbound HTTPS (no tunnel)
status_service ─────────────────────────────────────► mmomaid.cloud
                                                       /status        → 302 status.mmomaid.work
                                                       /status/api*   → public, consumed by prober
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Cinematic status page (HTML) |
| GET | `/api` | Current per-service status, overall, SLA — schema-compatible with platform |
| GET | `/api/graph?hours=6` | Response-time timeseries |
| GET | `/api/timeline?days=90` | Daily uptime % per service |
| GET | `/api/shards` | Bot cluster + per-shard status |
| GET | `/api/incidents?days=7` | Recent resolved incidents |
| GET | `/badge.svg` | Embeddable status badge (Shields.io style) |
| GET | `/health` | Lightweight liveness for Docker healthcheck |
| POST | `/admin/announce` | (HMAC) Create maintenance/incident banner |
| POST | `/admin/announce/{id}/update` | (HMAC) Append "investigating/identified/monitoring/resolved" update |
| POST | `/admin/announce/{id}/resolve` | (HMAC) Close the announcement |
