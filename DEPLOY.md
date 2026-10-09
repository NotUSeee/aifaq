# status_service — Ubuntu deployment runbook

End-to-end install: from a freshly-prepared FAQ host (already running
`cloudflared` + Docker + the FAQ AI on port 8080) to a working
`https://status.yourbot.work` exposed via the same Cloudflare Tunnel.

Total time: **~10 minutes**.

## Prerequisites

The Ubuntu host must already have:

- Docker + `docker compose` plugin
- `cloudflared` running with `/etc/cloudflared/config.yml` (the FAQ install set this up)
- `sqlite3`, `curl`, `rsync`, `openssl`, `jq` (`apt-get install -y sqlite3 curl rsync openssl jq`)
- ≥ 2 GB free on `/opt`

## What this installer does

1. Creates `/opt/status` and `/etc/status` (chmod 700).
2. Generates `/etc/status/.env` from `.env.example` with a fresh `ADMIN_HMAC_SECRET`.
3. Backs up `/etc/cloudflared/config.yml` and inserts the `status.yourbot.work` ingress rule.
4. Validates the cloudflared config; aborts and restores the backup on failure.
5. Calls `cloudflared tunnel route dns <tunnel> status.yourbot.work` to create the CNAME.
6. Reloads cloudflared (graceful — FAQ stays up).
7. Verifies FAQ is still healthy after reload; aborts and restores the backup if not.
8. Installs the systemd unit and starts the container.
9. Polls the local healthcheck and the public URL.

Idempotent — re-running is safe. Each step checks "already done?" first.

## One-command install

```bash
git clone <repo>            # or rsync the status_service directory to the host
cd status_service
sudo ./setup-host.sh
```

That's it. The script prints the next-steps when done.

## Configuring the env file

After install, edit `/etc/status/.env` to fill in optional integrations:

```ini
# Discord webhook for alerts (optional but strongly recommended)
ALERT_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Heartbeat — paste a healthchecks.io ping URL here
HEARTBEAT_PING_URL=https://hc-ping.com/<uuid>

# How a failed website check is confirmed (defaults shown; usually leave alone)
PROBE_ATTEMPTS=3
MONITOR_CONTROL_URLS=https://www.gstatic.com/generate_204,https://cloudflare.com/cdn-cgi/trace

# Litestream backups to Cloudflare R2 (optional)
LITESTREAM_REPLICA_URL=s3://mmo-maid-status-backups/status.db?endpoint=https://<account-id>.r2.cloudflarestorage.com&region=auto&force-path-style=true
LITESTREAM_ACCESS_KEY_ID=<your-r2-key>
LITESTREAM_SECRET_ACCESS_KEY=<your-r2-secret>
```

After editing, restart:

```bash
sudo systemctl restart status-compose
```

## Optional: Cloudflare WAF bypass for the prober

The status_service probes `yourbot.gg` from the Ubuntu host with
`User-Agent: yourbot-status/1.0 (+https://status.yourbot.work)`. If the
`yourbot.gg` Cloudflare zone has Bot Fight Mode or aggressive WAF
rules, probes may be challenged. To prevent that:

1. Cloudflare dashboard → yourbot.gg zone → Security → WAF → Custom rules
2. Create rule:
   - **When**: `(http.user_agent contains "yourbot-status/")`
   - **Then**: Skip → All managed rules
3. Save & deploy.

## Optional: Cloudflare static fallback Worker

So a status_service outage shows a friendly page instead of a 502:

1. Cloudflare dashboard → Workers & Pages → Create
2. Paste the Worker code from `deploy/cloudflare-fallback.js` (TODO: ship this)
3. Bind the Worker to route: `status.yourbot.work/*`
4. Set the Worker to handle origin failures.

## Cron: nightly cold backups

```cron
0 3 * * * /opt/status/scripts/backup.sh >> /var/log/maid-status-backup.log 2>&1
```

## Rolling forward changes

When new code lands:

```bash
cd ~/status_service
git pull
sudo ./setup-host.sh
```

The script copies the new source, builds the Docker image, copies the
database, and only then restarts the container. The page is down for the
few seconds of the swap, and a failed build or a failed copy stops the
script with the old page still serving. Cloudflared is not touched on
re-runs.

The database copy lands next to the database, named after the release that
was running: `/opt/status/data/status.db.before-upgrade.1.0.0` for the
upgrade to 1.1.0. It is a full copy. Delete it once you are sure you will
not go back.

Then check it from the box:

```bash
# running the new version
curl -s http://127.0.0.1:8081/health

# the page and its refresh are compressed by the service (expect "gzip"
# and a size near 10 KB, against about 165 KB without the header)
curl -s -H 'Accept-Encoding: gzip' -o /dev/null \
     -w '%{size_download} bytes\n' -D - http://127.0.0.1:8081/live | grep -i -E 'content-encoding|bytes'

# and from outside, the page answers and refreshes
curl -s -o /dev/null -w '%{http_code}\n' https://status.yourbot.work/live
```

## Switching on the reports sent to this service

Both are optional and independent. Nothing changes on the page until a
sender is actually reporting.

```bash
# 1. make two secrets and add them to /etc/status/.env
echo "INGEST_PLATFORM_SECRET=$(openssl rand -hex 32)" | sudo tee -a /etc/status/.env >/dev/null
echo "INGEST_VANTAGE_SECRET=$(openssl rand -hex 32)"  | sudo tee -a /etc/status/.env >/dev/null
sudo systemctl restart status-compose
```

- **Platform report.** Give the platform the same `INGEST_PLATFORM_SECRET`
  as `RR_STATUS_PUSH_SECRET`, and set
  `RR_STATUS_PUSH_URL=https://status.yourbot.work/ingest/platform` for the
  process that runs its health checker. From then on the page keeps showing
  the bot's real state while the website is down.
- **Other places.** Deploy `deploy/vantage-worker/` (its README has the
  three commands) with the same `INGEST_VANTAGE_SECRET`. One worker is one
  more place. The page names every place that has reported in the last ten
  minutes.

Check that they arrive: `curl -s https://status.yourbot.work/api | jq .meta.places,.meta.platform_reports_directly`

## Correcting stored history (after the measuring rework)

Before the rework the prober wrote every service `down` whenever its one
outside check of the website failed, and it believed a single failed
request with no retry. `remeasure` re-applies the current rules to what is
already stored. See the module docstring for exactly what it changes and
what it never touches.

Run it once, AFTER the new code is running (the old prober would keep
writing the rows it removes):

```bash
# 1. Read what it would do. Nothing is written.
sudo docker exec maid-status python -m status_service.remeasure

# 2. If the report looks right, apply. It first copies the database to
#    /opt/status/data/status.db.bak-<time>.
sudo docker exec maid-status python -m status_service.remeasure --apply
```

The report lists, for the website check, how many failed checks stay as
downtime (the platform answered with an error; connected but got no answer;
could not connect and the next check failed too) and how many stop counting
(the monitor could not look the domain up; could not connect once with good
checks either side). Pass `--keep-isolated` to keep that last group counted.

To undo it, stop the container and put the backup back:

```bash
sudo systemctl stop status-compose
sudo cp /opt/status/data/status.db.bak-<time> /opt/status/data/status.db
sudo rm -f /opt/status/data/status.db-wal /opt/status/data/status.db-shm
sudo systemctl start status-compose
```

Running it a second time finds nothing to do. Delete the `.bak-` file once
you are happy (it is a full copy, about the size of the database).

It only re-reads checks made by the old prober. The report says from which
moment on checks are left alone: that is when the current release first
ran here. A failure recorded after it was retried and confirmed when it
happened, so running this tool later can never remove it.

## Rolling back

If a deploy regresses anything:

1. Put the previous code back and reinstall:
   ```bash
   cd ~/status_service
   git checkout <previous commit>      # 0b57488 is the release before 1.1.0
   sudo systemctl stop status-compose
   # going back past 1.1.0? do the database step below BEFORE the next line
   sudo ./setup-host.sh
   ```
   **Going back past 1.1.0 needs one more step.** 1.1.0 moves the database
   to schema 7, and older code refuses to start on a newer schema ("Refusing
   to downgrade schema"). With the service stopped, either keep everything
   measured since the upgrade:
   ```bash
   sudo -u nobody sqlite3 /opt/status/data/status.db "UPDATE schema_version SET version=6;"
   ```
   (schema 7 only added one empty column and one table, which older code
   ignores), or put back the copy taken before the upgrade:
   ```bash
   sudo -u nobody cp /opt/status/data/status.db.before-upgrade.1.0.0 /opt/status/data/status.db
   sudo rm -f /opt/status/data/status.db-wal /opt/status/data/status.db-shm
   ```
   Run these as `nobody` (the user the container runs as). Opening the
   database as root while the service is stopped can leave files the
   container cannot write.
2. If the cloudflared config edit broke the FAQ:
   ```bash
   sudo cp /etc/cloudflared/config.yml.bak.<timestamp> /etc/cloudflared/config.yml
   sudo systemctl reload cloudflared
   ```

## Updating the platform's `/status` redirect

On the host that serves `yourbot.gg`, set the env var:

```bash
RR_STATUS_EXTERNAL_URL=https://status.yourbot.work
```

then restart the dashboard service. From then on, `https://yourbot.gg/status`
returns a 302 to the new URL. `/status/api*` endpoints stay open and
public so the external prober can keep consuming them.

## Quarterly restore drill

A backup that's never restored isn't a backup. Every quarter:

```bash
# Pick a recent snapshot
ls /opt/status/data/backups/daily/
# Restore to a sandbox path and verify row counts
mkdir -p /tmp/status-test
gunzip -c /opt/status/data/backups/daily/status-<date>.db.gz > /tmp/status-test/status.db
sqlite3 /tmp/status-test/status.db "SELECT COUNT(*) FROM probe_results"
```

If the count looks reasonable, the restore path works.

## Troubleshooting

| Symptom | Diagnosis | Fix |
|---|---|---|
| `https://status.yourbot.work` 522 | cloudflared can't reach 8081 | `docker ps`; if container missing, `systemctl restart status-compose` |
| Page says results "may be out of date" | Prober not running | `docker logs maid-status` — look for asyncio errors |
| Page says "Status checks paused" | The monitor cannot reach the internet (no control endpoint answered) | Check the host's connection; `docker logs maid-status` shows `monitor offline`. Nothing is recorded as downtime meanwhile |
| Everything but the website shows "No data" | The website check failed, so `/status/api` cannot be read | Expected during a website outage; clears on the first good check |
| Discord webhook not firing | URL wrong or alerts disabled | `curl -X POST $ALERT_DISCORD_WEBHOOK_URL -d '{"content":"test"}'` |
| Heartbeat stopped | Same as above (prober down) | Check `journalctl -u status-compose --since "1h ago"` |
| FAQ health check failing post-install | cloudflared config edit broke something | Restore backup: `cp /etc/cloudflared/config.yml.bak.<ts> /etc/cloudflared/config.yml; systemctl reload cloudflared` |
| `cloudflared tunnel ingress validate` fails | YAML syntax error | Restore backup, edit by hand, re-validate |
| New deploy didn't pick up `.env` changes | Container env_file not reloaded | `systemctl restart status-compose` (not `reload`) |
