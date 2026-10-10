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

Two more rules apply once the senders described under "Reports sent to this
service" are switched on:

4. **One place failing is not an outage.** When other places also check the
   website, it counts as down only when most of them cannot reach it. A lone
   failure, ours included, is that place's route.
5. **A down website does not hide the rest.** The platform sends its own
   report to this service directly, so the bot, the workers and the database
   keep their real state while the website is unreachable. Without that
   report they are `unknown`, as in rule 2.

The headline never says "All systems operational" on partial knowledge. If
nothing is failing but every check of an everyday component has no data, it
reads "Some systems are not reporting" and names them.

`python -m status_service.remeasure` re-applies these rules to stored
history (dry run by default; see DEPLOY.md). It only re-reads checks made
before these rules took over. The database remembers that moment, and a
failure recorded since is already confirmed, so it is never rewritten.

## Components

`components.py` is the catalog. Every check keeps the name the platform
reports it under (Gateway, Bot Worker, Database...) and is grouped into the
customer-facing components the page leads with:

| Component | Checks behind it |
|---|---|
| Website and dashboard | Public Site (outside), Dashboard, DNS*, SSL Certificate* |
| YourBot in Discord | Gateway, plus one check per shard once there are several |
| Commands and automations | Bot Worker, Bot, plus the live test (outside) when it is switched on |
| Custom bots | Orchestrator |
| Marketplace plugins | Plugin Runner, Sandbox, WebSocket Broker |
| Analytics | Analytics |
| Images | Image Service |
| Data storage | Database, Cache |
| Developer portal | Dev Portal Runner, Dev Portal Bot |
| Support assistant | FAQ Matcher |
| Discord (third party) | Discord's status page, optional token probe |

\* diagnostics: shown, but never part of an uptime figure.

**Shards.** The platform's Gateway check is about the gateway as a whole.
Once the shared bot runs on more than one shard, each shard becomes a check
of its own ("Shard 0", "Shard 1", ...) under "YourBot in Discord": one dead
shard is a partial outage, it is listed as an incident, and the component
takes the uptime of its worst shard. With a single shard nothing is added,
because the Gateway check already is that shard.

**The live test.** Every other check looks at one part: a process runs, a
shard is connected, a queue has a reader. All of them can pass while nothing a
server does gets answered. With `LIVE_TEST_WEBHOOK_URL` set, this service posts
a message through a Discord webhook into a private server every minute and
waits for the shared bot to react to it (`probes/live_test.py`). The reaction
only appears when Discord delivered the message to the gateway, the gateway
queued it, a worker picked it up and queued the reaction, and the bot sent it.

* a reaction within 10 s is operational (`LIVE_TEST_SLOW_SECONDS`)
* a later one is tested a second time with a new message before it is degraded
* no reaction is tested a second time with a new message before it is down
* a test that could not be run (Discord refused the message, or it could not
  be read back) is "no data", never down
* a missing reaction while Discord's own API or gateway is in trouble is "no
  data" too: we cannot tell whose failure it is
* until the bot has answered once through that webhook, a missing reaction is
  "no data" as well: a test that has never worked is a setup that is not
  finished, and that must not be published as an outage

It counts toward uptime like any other check of that component. The webhook's
address is the only secret it needs, and it is never logged or stored. The
platform has to be told which channel and webhook to answer in (see DEPLOY.md).

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

### One shard down among several

Closed on this side since 1.2.0: each shard is a check of its own (see
"Shards" above), so one dead shard is in the verdict, the uptime and the
incidents.

The platform's own Gateway check is a separate matter. Until yourbot PR #647
ships it reads `max(heartbeat_at)` and passes while ANY shard's heartbeat is
fresh. With that PR it judges every shard and reports "degraded" when only
some are connected. Either way the per-shard checks here do not depend on it.

## Reports sent to this service

Everything else on the page is fetched by this service. Three things can also
be sent to it, each signed with its own secret (`ingest.py`). Each route is
off, and answers 404, until its secret is set.

| Route | Sender | Secret | What it is for |
|---|---|---|---|
| `POST /ingest/platform` | the platform's health checker, once a minute | `INGEST_PLATFORM_SECRET` | The platform's own report (`{"status": <GET /status/api>, "shards": <GET /status/api/shards>}`), sent without going through the website. Used whenever this service cannot fetch it. |
| `POST /ingest/vantage` | a small checker in another network, once a minute | `INGEST_VANTAGE_SECRET` | One website check from somewhere else: `{"vantage": "cloudflare", "label": "Cloudflare", "status": "operational" or "down", "http_status": 200, "response_ms": 180, "error": null}`. Up to 8 places. |

Signing is the scheme the admin API uses: `X-Status-Timestamp` (unix
seconds) and `X-Status-Signature` = hex HMAC-SHA256 of
`"<timestamp>." + body`. A request older than two minutes is refused, and an
older report never replaces a newer one. Only the latest report of each
sender is kept; the scheduler reads it at its next cycle, so history keeps
one row per service per minute however a reading arrived. A report older
than about two minutes is ignored, and the page stops naming a place ten
minutes after its last report.

`deploy/vantage-worker/` holds a ready-made checker for Cloudflare Workers.

**Releases** (`POST /ingest/release`, `INGEST_RELEASE_SECRET`). The deploy
pipeline sends `{"action": "start", "version": "prod-1a2b3c4",
"expected_minutes": 20}` right before it restarts anything and
`{"action": "finish", "version": "prod-1a2b3c4", "result": "done" or
"failed"}` after. In between the page shows "A new version of YourBot is being
released right now", and an incident that begins in a release, or within five
minutes after one, is marked as such.

* It changes no verdict and no uptime figure. An incident in a release counts
  like any other. The mark says when it began, not why.
* The build's tag is stored but never shown: it is not the version in the
  patch notes.
* A pipeline that dies cannot leave the notice up: it comes down by itself
  after `expected_minutes` (kept between 5 and 180).
* Nothing is sent to subscribers and nothing is added to the RSS feed.
* The staff alert board in Discord says "Release in progress" and names the
  build, because the people reading it are the ones deploying.
* Sending twice is safe ("start" again only moves the expiry, a "finish" for
  a release that is not open changes nothing). The very same signed request is
  accepted once, so a captured "start" cannot be replayed after the release
  ended. Events are NOT ordered by the sender's clock, because two machines
  send them (the build, and whoever runs the deploy script).
* Releases are rows in their own table. That table needs no schema version
  bump, so 1.3.0 rolls back to 1.2.0 by swapping the image and nothing else.
  1.3.1 only changes how a late live-test answer is judged and stores nothing
  new, so it rolls back to 1.3.0 the same way.

The sender lives in the platform repository (`infra/status_release.py`, called
from `cloudbuild.yaml` and `infra/deploy-prod.sh`).

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

228 tests covering probes, the probe cycle (retries, monitor self-check, no
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
| POST | `/ingest/platform` | (HMAC) The platform's own health report, sent directly |
| POST | `/ingest/vantage` | (HMAC) A website check made from another place |
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
