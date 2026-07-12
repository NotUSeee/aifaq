# status_service — external status page for YourBot

`status.yourbot.work` runs on the user's Ubuntu home server, alongside
the FAQ AI, exposed via the same Cloudflare Tunnel. It probes
`yourbot.gg` from outside every 60s, stores history in SQLite, and
serves the public-facing status site.

When `yourbot.gg` is up, the prober proxies through to
`/status/api*` for internal-tier service data (DB, Redis, workers, bot
shards). When `yourbot.gg` is down, the page itself stays online —
because it's on a different machine — and records the full outage.

## Run locally for development

```bash
python -m venv .venv
source .venv/bin/activate           # on Windows: .venv\Scripts\activate
pip install -e .[dev]

# Point at staging or your dev YourBot instance
export PROBE_BASE_URL=https://yourbot.gg
export DB_PATH=./data/status.db
export ADMIN_HMAC_SECRET="$(openssl rand -hex 32)"

uvicorn status_service.main:app --reload --port 8081
# Open http://127.0.0.1:8081
```

## Run tests

```bash
pytest
```

72 tests covering probes, aggregator, alerter, badge, admin auth,
maintenance windows, feed, and the API contract.

## Deploy

See [DEPLOY.md](./DEPLOY.md) for the Ubuntu installer runbook.

## Architecture

```
visitors ─HTTPS─► Cloudflare Tunnel ─► cloudflared ─► status_service (:8081)
                                                       │
                                                       ├─ probe loop (60s)
                                                       │   ├─ HTTPS  /readiness (Public Site + derives Database/Cache)
                                                       │   ├─ PROXY  /status/api, /status/api/shards
                                                       │   ├─ DNS    resolve yourbot.gg
                                                       │   ├─ SSL    cert expiry (hourly)
                                                       │   └─ DISCORD API reachability (optional, own component)
                                                       ├─ alerter   (Discord webhook, SLA, SSL)
                                                       ├─ retention (daily prune of probe_results >30d + VACUUM)
                                                       └─ SQLite (probe_results, incidents, daily_uptime,
                                                                  shard_snapshot, announcements, alert_state)
                outbound HTTPS (no tunnel)
status_service ─────────────────────────────────────► yourbot.gg
                                                       /status        → 302 status.yourbot.work
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
| GET | `/feed.xml` | RSS feed — announcements + explained incidents, permalinked to page anchors |
| POST | `/admin/announce` | (HMAC) Create maintenance/incident banner; maintenance accepts `starts_at`/`ends_at` (UTC ISO) for scheduled windows |
| POST | `/admin/announce/{id}/update` | (HMAC) Append "investigating/identified/monitoring/resolved" update |
| POST | `/admin/announce/{id}/resolve` | (HMAC) Close the announcement |
| POST | `/admin/incident/{id}/cause` | (HMAC) Attach a public root-cause to an auto-detected incident |
| GET/POST | `/admin` | Web admin panel (username + password + TOTP): announcements, incident causes, staff |

Scheduled maintenance: a maintenance announcement with a future `starts_at`
shows under a calm "Scheduled" card (not a live banner), flips to an active
banner once the window opens, and auto-resolves when `ends_at` passes.
Timestamps render in the visitor's local timezone (UTC kept in tooltips).
