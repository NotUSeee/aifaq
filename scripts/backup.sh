#!/usr/bin/env bash
# Cold-snapshot backup. Runs nightly via cron at 3 AM:
#   0 3 * * * /opt/status/scripts/backup.sh >> /var/log/maid-status-backup.log 2>&1
# Pulls /etc/status/.env for RCLONE_REMOTE + BACKUP_BUCKET.

set -euo pipefail

ENV_FILE="${ENV_FILE:-/etc/status/.env}"
DB_FILE="${DB_FILE:-/opt/status/data/status.db}"
BACKUP_DIR="${BACKUP_DIR:-/opt/status/data/backups}"
RETENTION_DAILY=30
RETENTION_MONTHLY=12

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ "${BACKUP_ENABLED:-0}" != "1" ]]; then
  echo "BACKUP_ENABLED is not 1 — exiting."
  exit 0
fi

mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/monthly"
stamp="$(date +%F)"
out="$BACKUP_DIR/daily/status-${stamp}.db.gz"

echo "[$(date -Is)] Snapshotting $DB_FILE → $out"
sqlite3 "$DB_FILE" ".backup '$BACKUP_DIR/daily/status-${stamp}.db'"
gzip -f "$BACKUP_DIR/daily/status-${stamp}.db"

# Promote to monthly snapshot on the 1st of each month
if [[ "$(date +%d)" == "01" ]]; then
  cp "$out" "$BACKUP_DIR/monthly/status-${stamp}.db.gz"
fi

# Prune old daily/monthly backups
find "$BACKUP_DIR/daily" -name 'status-*.db.gz' -mtime +"$RETENTION_DAILY" -delete
find "$BACKUP_DIR/monthly" -name 'status-*.db.gz' -mtime +$((RETENTION_MONTHLY * 31)) -delete

# Sync to remote bucket if configured
if [[ -n "${RCLONE_REMOTE:-}" && -n "${BACKUP_BUCKET:-}" ]]; then
  echo "[$(date -Is)] Syncing to ${RCLONE_REMOTE}:${BACKUP_BUCKET}"
  rclone sync "$BACKUP_DIR" "${RCLONE_REMOTE}:${BACKUP_BUCKET}" \
    --transfers 4 --checkers 4 --quiet
fi

echo "[$(date -Is)] Backup complete."
