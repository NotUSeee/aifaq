# status_service — external status page for YourBot

`status.yourbot.work` runs on the Ubuntu home server, alongside the FAQ AI,
exposed via the same Cloudflare Tunnel. It checks `yourbot.gg` from outside
every 60s, stores history in SQLite, and serves the public status site.

When `yourbot.gg` is up, the prober also reads `/status/api*` for the
platform's own view of its services (database, cache, workers, bot shards).
When `yourbot.gg` is down, the page stays online, because it is on a
different machine, and records the outage.

## What counts as downtime

The page's job is to report what happened. Three rules keep it honest:

1. **A failure is confirmed before it is believed.** The website check is
   retried (`PROBE_ATTEMPTS`, default 3). If every attempt fails, the monitor
   then checks its own internet connection against `MONITOR_CONTROL_URLS`.
   If none of those answer, the fault is the monitor's: the cycle is stored
   as `unknown` ("no data"), never as downtime.
2. **Nothing is guessed.** When the website is confirmed unreachable, the
   services behind it cannot be read. They are stored as `unknown`, not
   `down`. (They used to be written `down`, which copied every website blip
   onto the bot, the gateway, the database and ten others.)
3. **Uptime only counts completed checks.** `unknown` is left out of the
   sum: it is neither up nor down.

Incidents are shown one per event, not one per affected check, and
interruptions under two minutes count toward uptime but are not listed.

`python -m status_service.remeasure` re-applies these rules to stored
history (dry run by default; see DEPLOY.md).

## Components

`components.py` is the catalog. Every check keeps the name the platform
reports it under (Gateway, Bot Worker, Database...) and is grouped into the
customer-facing components the page leads with:

| Component | Checks behind it |
|---|---|
| Website and dashboard | Public Site (outside), Dashboard, DNS*, SSL Certificate* |
| YourBot in Discord | Gateway |
| Commands and automations | Bot Worker, Bot |
| Custom bots | Orchestrator |
| Marketplace plugins | Plugin Runner, Sandbox, WebSocket Broker |
| Analytics | Analytics |
| Images | Image Service |
| Data storage | Database, Cache |
| Developer portal | Dev Portal Runner, Dev Portal Bot |
| Support assistant | FAQ Matcher |
| Discord (third party) | Discord's status page, optional token probe |

\* diagnostics: shown, but never part of an uptime figure.

A component's uptime is that of its weakest check. "Overall uptime" is the
mean of the core components (everything except Developer portal, Support
assistant and Discord). Discord never affects YourBot's verdict or uptime.

Every component has a 90-day bar, one cell per UTC day. Open "the checks
behind this" and each check has its own bar too (where a component rests on
one check, the component's bar is that check's).

## What yourbot.gg says about this page

The main site describes this page to customers, so these are promises the
page has to keep. Change the page and the site's copy together, never one
alone. Each line is restated as a test (`test_claim_*` in
`tests/test_reporting.py`).

| Where | What it says | Kept by |
|---|---|---|
| `/discord-bots/hosting` and its FAQ | needs no login | no auth on any public route |
| same | lists each part of the platform on its own, from the website and dashboard through the gateway, bot workers and plugin runner to the database and cache | every check is listed under its component |
| same | each part has a daily uptime bar for the last 90 days | component bars and per-check bars |
| same | refreshes itself every 15 seconds | `REFRESH_MS` in `static/status.js` |
| same | incidents: the last 7 days on the front page, 90 days in the history | `/` and `/history` |
| same | follow it by RSS or send updates to your own Discord channel with a webhook | `/feed.xml`, `/subscribe/webhook` |
| `/about` | live uptime, incident history and shard health | "Shards online" under Right now, and each shard listed once there are several |
| `/security` | uptime and incident history are public | as above |

### Known gap: one shard down among several

The verdict for "YourBot in Discord" comes from the platform's Gateway check,
which passes while ANY shard's heartbeat is fresh (`_check_gateway` reads
`max(heartbeat_at)`). With one shard, as today, that is the whole bot. Once
the shared bot runs on several, one dead shard would leave the headline on
"All systems operational" while the servers on that shard get no response.
The page would still show it under Right now ("3 of 4 shards online, 1
down", and the shard in the list), but not in the verdict, the uptime or the
incidents. Close this before sharding goes live: have the platform check
look at every shard, and decide how a partial shard outage counts toward
uptime.

## Run locally for development

```bash
python -m venv .venv
source .venv/bin/activate           # on Windows: .venv\Scripts\activate
pip install -e .[dev]

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

172 tests covering probes, the probe cycle (retries, monitor self-check, no
guessed downtime), uptime and incident aggregation, the page and its live
fragment, what yourbot.gg says about the page, the history-correction tool,
alerter, badge, admin auth, maintenance windows, feed, and the API contract.

## Deploy

See [DEPLOY.md](./DEPLOY.md) for the Ubuntu installer runbook.

## Architecture

```
visitors ─HTTPS─► Cloudflare Tunnel ─► cloudflared ─► status_service (:8081)
                                                       │
                                                       ├─ probe loop (60s)
                                                       │   ├─ HTTPS  /readiness, retried (Public Site)
                                                       │   ├─ SELF   control endpoints, only after a failure
                                                       │   ├─ PROXY  /status/api, /status/api/shards
                                                       │   ├─ DNS    resolve yourbot.gg (diagnostic)
                                                       │   ├─ SSL    cert validity + expiry, hourly (diagnostic)
                                                       │   ├─ DISCORD status page summary (third party)
                                                       │   └─ DISCORD API reachability (optional, needs a token)
                                                       ├─ alerter   (Discord: live status-board message edited
                                                       │             in place; ALERT_STYLE=stream for legacy
                                                       │             per-event posts; SLA + SSL daily pings)
                                                       ├─ retention (daily prune of probe_results >30d + VACUUM)
                                                       └─ SQLite (probe_results, incidents, daily_uptime,
                                                                  daily_uptime_orig, shard_snapshot,
                                                                  announcements, alert_state)
                outbound HTTPS (no tunnel)
status_service ─────────────────────────────────────► yourbot.gg
                                                       /status        → 302 status.yourbot.work
                                                       /status/api*   → public, consumed by prober
```

## Look and theme

The page is a yourbot.gg page: the same nav bar, page-header form, cards,
buttons, mono-caps tags, stat strip, starfield and footer, in both the dark
and the light theme, with the site's theme toggle (follows the system until
a choice is made; `localStorage['mmo_theme']`, per site). Because it has to
render while yourbot.gg is down it cannot load the site's stylesheets, so
`static/status.css` restates the site's design tokens (tokens.css v10) and
`static/sky.js` is a verbatim copy of the site's starfield. When the site's
tokens change, change them here too. Before shipping visual changes, compare computed
styles against the live site in both themes.

The page is drawn entirely by the server (templates/_regions.html). The
script swaps the live regions in from `/live` every 15s, so the first paint
and every refresh are drawn the same way, and the page works without
scripts. The response-time chart is inline SVG; there is no chart library.

Responses are gzip-compressed by the service itself. The page is mostly
90-day bars, so the 15-second refresh travels as about 10 KB instead of
about 165 KB. That matters because every open tab pulls it over the home
server's own uplink, and most of all during an incident.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Status page (HTML) |
| GET | `/live` | The page's live regions as one HTML fragment (polled by the page script) |
| GET | `/history` | 90-day incident + maintenance archive, one entry per event, with causes |
| GET | `/api` | `components` (customer-facing, uptime per period), `current` (per check), `overall`, `meta` |
| GET | `/api/graph?hours=24` | Website response-time percentiles (bucketed p50/p95) |
| GET | `/api/timeline?days=90` | Daily uptime per check (`series`) and per component (`groups`) |
| GET | `/api/shards` | Shared-bot shard status |
| GET | `/api/incidents?days=7` | `events` (grouped) and `incidents` (raw per-check rows) |
| GET | `/badge.svg` | Embeddable overall badge (Shields.io style) |
| GET | `/badge/{slug}.svg` | Badge for one check (`/badge/plugin-runner.svg`) or one component (`/badge/custom-bots.svg`) |
| GET | `/health` | Lightweight liveness for Docker healthcheck |
| POST | `/subscribe/webhook` | Register a Discord webhook to receive announcement broadcasts (validated + test ping) |
| GET/POST | `/subscribe/unsubscribe` | Token-authorized unsubscribe (link included in every delivery) |
| GET | `/feed.xml` | RSS feed: announcements + incidents (one item per event), permalinked to page anchors |
| POST | `/admin/announce` | (HMAC) Create maintenance/incident banner; maintenance accepts `starts_at`/`ends_at` (UTC ISO) for scheduled windows |
| POST | `/admin/announce/{id}/update` | (HMAC) Append "investigating/identified/monitoring/resolved" update |
| POST | `/admin/announce/{id}/resolve` | (HMAC) Close the announcement |
| POST | `/admin/incident/{id}/cause` | (HMAC) Attach a public cause to one incident row |
| GET/POST | `/admin` | Web admin panel (username + password + TOTP): announcements, incident causes (one per event), staff |

Scheduled maintenance: a maintenance announcement with a future `starts_at`
shows under a calm "Scheduled" card (not a live banner), flips to an active
banner once the window opens, and auto-resolves when `ends_at` passes.
Timestamps render in the visitor's local timezone (UTC kept in tooltips).
