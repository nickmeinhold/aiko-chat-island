#!/usr/bin/env bash
# cert-restart.sh — BOOTSTRAP restart trigger (machine-forced renewal loop).
#
# Runs served-cert-alarm.sh; on exit 10 (STALE served cert) it restarts LiveKit so
# pion/turn reloads the renewed cert from the RO bind-mount. On exit 20 (endpoint
# down) it does NOT restart (the service is already dead — restarting won't fix a
# missing cert and would mask the real fault); the alarm has already paged.
#
# CLIENT+REPAIR (imagineering) does NOT install this — its restart is human-forced
# via the runbook state machine. This trigger is BOOTSTRAP-only (island-owned box).
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
: "${TURN_DOMAIN:?set TURN_DOMAIN}"
CERT_DIR="${CADDY_CERT_LEAF_DIR:?set CADDY_CERT_LEAF_DIR}"

set +e; ./served-cert-alarm.sh; rc=$?; set -e
case "$rc" in
  0)  echo "served cert healthy — no restart."; exit 0 ;;
  10) echo "served cert STALE — validating renewed material before restart…" ;;
  20) echo "endpoint DOWN — alarm paged; NOT restarting (not a cert-renewal)."; exit 0 ;;
  *)  echo "alarm returned unexpected rc=$rc — failing closed, no restart." >&2; exit 1 ;;
esac

# Validate-before-restart (Tesla r3 residual): refuse to restart onto a half-written
# or mismatched cert/key pair — restarting into a broken pair would take TURN down
# harder than a soon-to-expire cert. Fail CLOSED: bad pair => no restart, alarm stays.
crt="$CERT_DIR/${TURN_DOMAIN}.crt"; key="$CERT_DIR/${TURN_DOMAIN}.key"
if [ ! -s "$crt" ] || [ ! -s "$key" ]; then
  echo "renewed cert/key missing or empty at $CERT_DIR — refusing restart (fail-closed)." >&2; exit 1
fi
crt_mod="$(openssl x509 -noout -modulus -in "$crt" 2>/dev/null | openssl md5)"
key_mod="$(openssl rsa  -noout -modulus -in "$key" 2>/dev/null | openssl md5 \
           || openssl ec -noout -in "$key" 2>/dev/null | openssl md5)"
if [ -z "$crt_mod" ] || [ "$crt_mod" != "$key_mod" ]; then
  echo "cert/key modulus mismatch (half-write?) — refusing restart (fail-closed)." >&2; exit 1
fi

echo "renewed pair valid — restarting livekit."
docker restart livekit
# Re-run the alarm to confirm the served endpoint now presents the fresh cert.
sleep 5
./served-cert-alarm.sh
