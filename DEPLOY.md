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
cd ~/path/to/status_service
git pull
sudo ./setup-host.sh
```

The script copies the new source, rebuilds the Docker image, and restarts
the container. Cloudflared is not touched on re-runs.

## Rolling back

If a deploy regresses anything:

1. Revert `/opt/status/`:
   ```bash
   sudo rsync -a --delete /opt/status.bak/ /opt/status/
   sudo systemctl restart status-compose
   ```
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
| Page shows "STATUS DATA STALE" | Prober not running | `docker logs maid-status` — look for asyncio errors |
| Discord webhook not firing | URL wrong or alerts disabled | `curl -X POST $ALERT_DISCORD_WEBHOOK_URL -d '{"content":"test"}'` |
| Heartbeat stopped | Same as above (prober down) | Check `journalctl -u status-compose --since "1h ago"` |
| FAQ health check failing post-install | cloudflared config edit broke something | Restore backup: `cp /etc/cloudflared/config.yml.bak.<ts> /etc/cloudflared/config.yml; systemctl reload cloudflared` |
| `cloudflared tunnel ingress validate` fails | YAML syntax error | Restore backup, edit by hand, re-validate |
| New deploy didn't pick up `.env` changes | Container env_file not reloaded | `systemctl restart status-compose` (not `reload`) |
