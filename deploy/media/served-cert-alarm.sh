#!/usr/bin/env bash
# served-cert-alarm.sh — the cert-drift detector for BOTH modes.
#
# THE POINT (DESIGN §3.1, round-2 Carnot+Tesla FATAL): pion/turn loads its cert
# into MEMORY at boot, so the mounted file on disk can be freshly renewed while
# LiveKit still SERVES the stale in-memory cert on :5349. An `openssl x509 -in
# mounted.crt` check is green while the endpoint is stale — Jul-24 in runtime form.
# So we probe the ENDPOINT LiveKit actually presents, with SNI, and read THAT
# notAfter. Disk-live != process-live.
#
# Exit codes: 0 = served cert healthy (>= threshold). 10 = STALE (notAfter < N)
# -> renewal restart needed. 20 = endpoint DOWN (couldn't handshake) -> distinct
# from stale: the service is dead, page differently (Maxwell r3 residual).
#
# BOOTSTRAP: cert-restart.sh acts on exit 10. CLIENT+REPAIR (imagineering): this
# runs as a pure detector — it PAGES, never restarts (the runbook state machine
# owns the restart). Set ALARM_RESTART=0 to force detector-only.
set -euo pipefail

: "${TURN_DOMAIN:?set TURN_DOMAIN}"
: "${ALARM_NOTAFTER_DAYS:=14}"
: "${ALARM_NOTIFY_URL:=}"
TURN_TLS_PORT="${TURN_TLS_PORT:-5349}"
HOST="${TURN_PROBE_HOST:-127.0.0.1}"   # probe locally by default; override to test externally

notify() {  # $1 = severity, $2 = message
  local msg="[media/$TURN_DOMAIN] $1: $2"
  echo "$msg" >&2
  [ -n "$ALARM_NOTIFY_URL" ] && curl -fsS -m 10 -X POST "$ALARM_NOTIFY_URL" \
    --data-urlencode "text=$msg" >/dev/null 2>&1 || true
}

# Handshake the live TURNS endpoint and pull the SERVED leaf cert. `-servername`
# sets SNI so a name-based cert is selected exactly as a real client would get it.
served_cert="$(echo | timeout 15 openssl s_client -connect "${HOST}:${TURN_TLS_PORT}" \
                 -servername "$TURN_DOMAIN" 2>/dev/null \
               | openssl x509 2>/dev/null || true)"

if [ -z "$served_cert" ]; then
  notify PAGE "TURN TLS endpoint ${HOST}:${TURN_TLS_PORT} did not present a cert (service down or 5349 unreachable) — NOT a cert-renewal, investigate the process/firewall."
  exit 20
fi

not_after="$(echo "$served_cert" | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"
not_after_epoch="$(date -d "$not_after" +%s 2>/dev/null || date -jf '%b %d %T %Y %Z' "$not_after" +%s 2>/dev/null)"
now_epoch="$(date +%s)"
days_left=$(( (not_after_epoch - now_epoch) / 86400 ))

echo "served-cert notAfter=$not_after (${days_left}d left, threshold ${ALARM_NOTAFTER_DAYS}d)"

if [ "$days_left" -lt "$ALARM_NOTAFTER_DAYS" ]; then
  notify STALE "served cert has ${days_left}d left (< ${ALARM_NOTAFTER_DAYS}d) — Caddy likely renewed but LiveKit still serves the old in-memory cert; a restart is needed to pick up the renewal."
  exit 10
fi
exit 0
