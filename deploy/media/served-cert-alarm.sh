#!/usr/bin/env bash
# served-cert-alarm.sh — the cert-drift DETECTOR (both modes). Pure detector: it
# probes and pages; it does NOT restart (cert-restart.sh owns the restart decision).
#
# THE POINT (DESIGN §3.1): pion/turn serves its cert from MEMORY, so a renewed file
# on disk can coexist with a stale served cert on :5349. We probe the ENDPOINT with
# SNI and read THAT notAfter — an `openssl x509 -in file` check would be green while
# the endpoint is stale (Jul-24 in runtime form).
#
# Exit: 0 healthy · 10 served cert STALE (< N days) · 20 endpoint DOWN (no handshake).
# Paging is guaranteed-signal: if the notify POST fails, we drop a local flag file
# the box monitor scrapes, so a dead notify path cannot silence the alarm (round-1
# Tesla: best-effort `|| true` notify was itself a Jul-24-shaped silence).
set -euo pipefail
cd "$(dirname "$0")"; . lib/cert-pair.sh
: "${TURN_DOMAIN:?set TURN_DOMAIN}"
: "${ALARM_NOTAFTER_DAYS:=14}"
: "${ALARM_NOTIFY_URL:=}"
: "${ALARM_FLAG_DIR:=/var/local/media-alarm}"
TURN_TLS_PORT="${TURN_TLS_PORT:-5349}"
HOST="${TURN_PROBE_HOST:-127.0.0.1}"

notify() {  # $1 severity  $2 message — guaranteed signal (notify OR local flag)
  local msg="[media/$TURN_DOMAIN] $1: $2"
  echo "$msg" >&2
  if [ -n "$ALARM_NOTIFY_URL" ] && curl -fsS -m 10 -X POST "$ALARM_NOTIFY_URL" \
       --data-urlencode "text=$msg" >/dev/null 2>&1; then
    return 0
  fi
  # Notify failed (or unset) → durable local flag the existing box monitor scrapes.
  mkdir -p "$ALARM_FLAG_DIR" 2>/dev/null || ALARM_FLAG_DIR="$(dirname "$0")"
  printf '%s\n' "$msg" > "$ALARM_FLAG_DIR/${TURN_DOMAIN}.${1}.flag" 2>/dev/null || true
}

served_epoch="$(served_cert_not_after_epoch "$HOST" "$TURN_TLS_PORT" "$TURN_DOMAIN" || true)"
if [ -z "$served_epoch" ]; then
  notify PAGE "TURN TLS endpoint ${HOST}:${TURN_TLS_PORT} presented no cert (service down / 5349 unreachable) — NOT a renewal, investigate process/firewall."
  exit 20
fi
days_left=$(( (served_epoch - $(date +%s)) / 86400 ))
echo "served-cert notAfter epoch=$served_epoch (${days_left}d left, threshold ${ALARM_NOTAFTER_DAYS}d)"
if [ "$days_left" -lt "$ALARM_NOTAFTER_DAYS" ]; then
  notify STALE "served cert has ${days_left}d left (< ${ALARM_NOTAFTER_DAYS}d) — restart needed to pick up a renewal (or renewal pipeline is broken)."
  exit 10
fi
# Clear a prior flag once healthy again.
rm -f "$ALARM_FLAG_DIR/${TURN_DOMAIN}.STALE.flag" "$ALARM_FLAG_DIR/${TURN_DOMAIN}.PAGE.flag" 2>/dev/null || true
exit 0
