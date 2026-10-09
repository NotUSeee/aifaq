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

# Keep a copy of the database first. Do this before any release that
# changes its schema (1.1.0 moves it to schema 7). It runs inside the
# container that is serving right now, so the page stays up.
sudo docker exec maid-status python -c "import sqlite3; s=sqlite3.connect('/data/status.db'); d=sqlite3.connect('/data/status.db.before-upgrade'); s.backup(d); d.close(); s.close(); print('copy written')"

sudo ./setup-host.sh
```

The script copies the new source, builds the Docker image, and only then
restarts the container, so the page is down for the few seconds of the swap
and a failed build leaves the old page serving. Cloudflared is not touched
on re-runs.

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
   sudo -u nobody cp /opt/status/data/status.db.before-upgrade /opt/status/data/status.db
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
