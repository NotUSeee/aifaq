#!/usr/bin/env bash
# Restore a snapshot from local backups or the remote bucket.
# Usage:  ./restore.sh 2026-05-03            # local daily/status-2026-05-03.db.gz
#         ./restore.sh r2:status-2026-05-03  # pull from remote bucket first

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <date-or-r2-name>"
  exit 1
fi

ENV_FILE="${ENV_FILE:-/etc/status/.env}"
DB_FILE="${DB_FILE:-/opt/status/data/status.db}"
BACKUP_DIR="${BACKUP_DIR:-/opt/status/data/backups}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

target="$1"

if [[ "$target" == r2:* ]]; then
  name="${target#r2:}"
  remote_path="${RCLONE_REMOTE:-r2}:${BACKUP_BUCKET:-mmo-maid-status-backups}/daily/${name}.db.gz"
  echo "[$(date -Is)] Pulling $remote_path"
  rclone copy "$remote_path" "$BACKUP_DIR/daily/" --quiet
  src="$BACKUP_DIR/daily/${name}.db.gz"
else
  src="$BACKUP_DIR/daily/status-${target}.db.gz"
fi

if [[ ! -f "$src" ]]; then
  echo "Backup not found: $src" >&2
  exit 1
fi

echo "[$(date -Is)] Stopping container"
systemctl stop status-compose || true

echo "[$(date -Is)] Restoring $src → $DB_FILE"
gunzip -c "$src" > "${DB_FILE}.restore"
mv "${DB_FILE}.restore" "$DB_FILE"
chown 65534:65534 "$DB_FILE"

echo "[$(date -Is)] Starting container"
systemctl start status-compose

echo "Done. Verify at https://status.mmomaid.work/api"
