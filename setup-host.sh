#!/usr/bin/env bash
# setup-host.sh — install status_service on an Ubuntu host alongside the
# existing FAQ service. Idempotent: re-running is safe and only re-applies
# the parts that changed.
#
# Usage:  sudo ./setup-host.sh                # full install
#         sudo ./setup-host.sh --check        # dry-run, prints what would change
#         sudo ./setup-host.sh --rotate-admin-secret
#
# Pre-requisites already on the host (from the FAQ install):
#   - Docker + docker-compose-plugin
#   - cloudflared with /etc/cloudflared/config.yml routing faq.mmomaid.work

set -euo pipefail

INSTALL_DIR="/opt/status"
ENV_DIR="/etc/status"
ENV_FILE="${ENV_DIR}/.env"
SYSTEMD_UNIT="/etc/systemd/system/status-compose.service"
CF_CONFIG="/etc/cloudflared/config.yml"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY_RUN=0
ROTATE_SECRET=0
case "${1-}" in
  --check) DRY_RUN=1 ;;
  --rotate-admin-secret) ROTATE_SECRET=1 ;;
esac

log()  { echo -e "\033[1;36m▸\033[0m $*"; }
warn() { echo -e "\033[1;33m!\033[0m $*"; }
err()  { echo -e "\033[1;31m✗\033[0m $*" >&2; }
ok()   { echo -e "\033[1;32m✓\033[0m $*"; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    err "Run as root (sudo)"
    exit 1
  fi
}

precheck_disk() {
  local needed_kb=2097152  # 2 GB
  local avail_kb
  avail_kb="$(df -k /opt | tail -1 | awk '{print $4}')"
  if (( avail_kb < needed_kb )); then
    err "Less than 2 GB free on /opt — refusing to install. Free up space first."
    exit 1
  fi
}

precheck_docker() {
  if ! command -v docker >/dev/null; then
    err "docker not installed. Install docker + docker-compose-plugin first."
    exit 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    err "'docker compose' plugin not available."
    exit 1
  fi
}

precheck_faq_alive() {
  if ! curl -fsS --max-time 5 https://faq.mmomaid.work/health >/dev/null 2>&1; then
    warn "FAQ (https://faq.mmomaid.work) didn't respond healthy — continuing,"
    warn "but verify post-install that we didn't break it."
  fi
}

rotate_admin_secret() {
  require_root
  log "Rotating ADMIN_HMAC_SECRET in $ENV_FILE"
  if [[ ! -f $ENV_FILE ]]; then
    err "$ENV_FILE not found — run a full install first."
    exit 1
  fi
  local new_secret
  new_secret="$(openssl rand -hex 32)"
  sed -i.bak.$(date +%s) "s|^ADMIN_HMAC_SECRET=.*|ADMIN_HMAC_SECRET=${new_secret}|" "$ENV_FILE"
  systemctl restart status-compose
  ok "Rotated. Restart any clients that hold the old secret."
}

mkdirs() {
  log "Creating dirs: $INSTALL_DIR, $ENV_DIR, $INSTALL_DIR/data"
  if (( DRY_RUN == 0 )); then
    mkdir -p "$INSTALL_DIR" "$ENV_DIR" "$INSTALL_DIR/data"
    chown -R 65534:65534 "$INSTALL_DIR/data"
    chmod 700 "$ENV_DIR"
  fi
}

copy_source() {
  log "Copying status_service source to $INSTALL_DIR"
  if (( DRY_RUN == 0 )); then
    rsync -a --delete \
      --exclude=data --exclude=tests --exclude=.venv --exclude=__pycache__ \
      --exclude=.pytest_cache --exclude=*.bak.* \
      "$SOURCE_DIR/" "$INSTALL_DIR/"
  fi
}

write_env() {
  if [[ -f $ENV_FILE ]]; then
    ok "$ENV_FILE already exists — keeping in place. Edit manually to change settings."
    return
  fi
  log "Creating $ENV_FILE from .env.example"
  if (( DRY_RUN == 0 )); then
    cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
    local secret
    secret="$(openssl rand -hex 32)"
    sed -i "s|^ADMIN_HMAC_SECRET=.*|ADMIN_HMAC_SECRET=${secret}|" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "Generated ADMIN_HMAC_SECRET. Edit $ENV_FILE to fill webhook + heartbeat URLs."
  fi
}

install_systemd() {
  log "Installing $SYSTEMD_UNIT"
  if (( DRY_RUN == 0 )); then
    cp "$SOURCE_DIR/deploy/status-compose.service" "$SYSTEMD_UNIT"
    systemctl daemon-reload
    systemctl enable status-compose >/dev/null
  fi
}

update_cloudflared() {
  if [[ ! -f $CF_CONFIG ]]; then
    err "$CF_CONFIG not found — is cloudflared set up?"
    exit 1
  fi
  if grep -q "status.mmomaid.work" "$CF_CONFIG"; then
    ok "cloudflared already routes status.mmomaid.work — no edit needed."
    return
  fi

  log "Backing up $CF_CONFIG"
  local backup
  backup="${CF_CONFIG}.bak.$(date +%Y%m%d%H%M%S)"
  if (( DRY_RUN == 0 )); then
    cp "$CF_CONFIG" "$backup"
    ok "Backup at $backup"

    log "Inserting status.mmomaid.work ingress rule above the catch-all"
    # Insert two lines before the `- service: http_status:404` line.
    awk '/^[[:space:]]*-[[:space:]]*service:[[:space:]]*http_status:404/ && !done {
        print "  - hostname: status.mmomaid.work"
        print "    service: http://localhost:8081"
        done=1
    }
    {print}' "$backup" > "$CF_CONFIG"

    log "Validating new config"
    if ! cloudflared tunnel ingress validate "$CF_CONFIG"; then
      err "Validation failed. Restoring backup."
      cp "$backup" "$CF_CONFIG"
      exit 1
    fi

    log "Telling Cloudflare to publish DNS for status.mmomaid.work"
    local tunnel_name
    tunnel_name="$(awk '/^tunnel:/ {print $2; exit}' "$CF_CONFIG")"
    # cloudflared looks for cert.pem in $HOME/.cloudflared by default. When
    # we run via sudo, $HOME=/root, but the cert was issued under the user
    # who ran `cloudflared tunnel login`. Find it across the common spots.
    local cert_path=""
    for candidate in /root/.cloudflared/cert.pem \
                     /etc/cloudflared/cert.pem \
                     "/home/${SUDO_USER:-$USER}/.cloudflared/cert.pem"; do
      if [[ -f "$candidate" ]]; then cert_path="$candidate"; break; fi
    done
    if [[ -z "$cert_path" ]]; then
      cert_path="$(find /home -maxdepth 4 -name cert.pem 2>/dev/null | head -1)"
    fi
    if [[ -n $tunnel_name ]]; then
      if [[ -n "$cert_path" ]]; then
        cloudflared --origincert "$cert_path" tunnel route dns "$tunnel_name" status.mmomaid.work || \
          warn "tunnel route dns returned non-zero — DNS may already exist (OK)."
      else
        warn "Could not find cert.pem — create the CNAME manually in the Cloudflare"
        warn "dashboard: status.mmomaid.work CNAME ${tunnel_name}.cfargotunnel.com"
      fi
    fi

    log "Restarting cloudflared (the package's systemd unit doesn't support reload)"
    systemctl restart cloudflared
    sleep 2
    if ! curl -fsS --max-time 5 https://faq.mmomaid.work/health >/dev/null; then
      err "FAQ regression check failed after reload — restoring config and reloading"
      cp "$backup" "$CF_CONFIG"
      systemctl reload cloudflared
      exit 1
    fi
    ok "FAQ still healthy after cloudflared reload."
  fi
}

start_status() {
  log "Starting status-compose service"
  if (( DRY_RUN == 0 )); then
    systemctl restart status-compose
    log "Waiting up to 60s for healthcheck"
    for _ in $(seq 1 30); do
      if curl -fsS --max-time 3 http://127.0.0.1:8081/health >/dev/null 2>&1; then
        ok "status_service is healthy locally."
        break
      fi
      sleep 2
    done

    log "Verifying public reachability"
    if curl -fsS --max-time 10 https://status.mmomaid.work/health >/dev/null; then
      ok "https://status.mmomaid.work is live."
    else
      warn "https://status.mmomaid.work not yet reachable — DNS may take ~60s."
    fi
  fi
}

main() {
  if (( ROTATE_SECRET == 1 )); then
    rotate_admin_secret
    return
  fi

  require_root
  precheck_disk
  precheck_docker
  precheck_faq_alive

  mkdirs
  copy_source
  write_env
  install_systemd
  update_cloudflared
  start_status

  ok "Done. Edit $ENV_FILE to configure Discord webhook + heartbeat."
  echo "    Visit https://status.mmomaid.work to see the page."
}

main "$@"
