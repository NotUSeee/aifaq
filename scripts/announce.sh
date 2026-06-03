#!/usr/bin/env bash
# CLI helper to post a maintenance/incident announcement to status.yourbot.work.
# Wraps the HMAC signing so you can post from your workstation.
#
# Usage:
#   ./announce.sh new <severity> "<title>" "<body>"
#       severity: info | warning | critical
#   ./announce.sh update <id> <status> "<body>"
#       status: investigating | identified | monitoring | resolved
#   ./announce.sh resolve <id>
#   ./announce.sh cause <incident_id> "<root-cause explanation>"
#       incident_id: from GET /api/incidents  (the auto-detected outage to explain)
#
# Required env:
#   STATUS_BASE_URL   default: https://status.yourbot.work
#   ADMIN_HMAC_SECRET (read from /etc/status/.env if present)

set -euo pipefail

BASE="${STATUS_BASE_URL:-https://status.yourbot.work}"
ENV_FILE="${ENV_FILE:-/etc/status/.env}"

if [[ -z "${ADMIN_HMAC_SECRET:-}" && -f "$ENV_FILE" ]]; then
  ADMIN_HMAC_SECRET="$(grep -E '^ADMIN_HMAC_SECRET=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
  export ADMIN_HMAC_SECRET
fi

if [[ -z "${ADMIN_HMAC_SECRET:-}" ]]; then
  echo "ADMIN_HMAC_SECRET is empty. Set it in your shell or in $ENV_FILE." >&2
  exit 1
fi

sign_and_post() {
  local path="$1"
  local body="$2"
  local ts
  ts="$(date +%s)"
  local sig
  sig="$(printf '%s.%s' "$ts" "$body" | openssl dgst -sha256 -hmac "$ADMIN_HMAC_SECRET" | awk '{print $2}')"
  curl -fsS -X POST "${BASE}${path}" \
    -H "Content-Type: application/json" \
    -H "X-Status-Timestamp: ${ts}" \
    -H "X-Status-Signature: ${sig}" \
    -d "$body"
  echo
}

cmd="${1:-}"
case "$cmd" in
  new)
    severity="$2"; title="$3"; body_text="$4"
    payload="$(jq -nc --arg sev "$severity" --arg t "$title" --arg b "$body_text" \
      '{type:"incident", severity:$sev, title:$t, body:$b}')"
    sign_and_post "/admin/announce" "$payload"
    ;;
  maintenance)
    severity="$2"; title="$3"; body_text="$4"
    payload="$(jq -nc --arg sev "$severity" --arg t "$title" --arg b "$body_text" \
      '{type:"maintenance", severity:$sev, title:$t, body:$b}')"
    sign_and_post "/admin/announce" "$payload"
    ;;
  update)
    id="$2"; status="$3"; body_text="$4"
    payload="$(jq -nc --arg s "$status" --arg b "$body_text" '{status:$s, body:$b}')"
    sign_and_post "/admin/announce/${id}/update" "$payload"
    ;;
  resolve)
    id="$2"
    sign_and_post "/admin/announce/${id}/resolve" ""
    ;;
  cause)
    id="$2"; cause_text="$3"
    payload="$(jq -nc --arg c "$cause_text" '{cause:$c}')"
    sign_and_post "/admin/incident/${id}/cause" "$payload"
    ;;
  *)
    echo "Usage:" >&2
    echo "  $0 new        <info|warning|critical> \"<title>\" \"<body>\"" >&2
    echo "  $0 maintenance <info|warning|critical> \"<title>\" \"<body>\"" >&2
    echo "  $0 update     <id> <investigating|identified|monitoring|resolved> \"<body>\"" >&2
    echo "  $0 resolve    <id>" >&2
    echo "  $0 cause      <incident_id> \"<root-cause explanation>\"" >&2
    exit 1
    ;;
esac
