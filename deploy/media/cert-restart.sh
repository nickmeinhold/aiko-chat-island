#!/usr/bin/env bash
# cert-restart.sh — renewal restart decision (BOOTSTRAP machine-forced loop).
#
# Two clocks (round-1 Tesla): "served cert stale" alone is NOT enough — restarting
# on notAfter<N every day while Caddy has NOT actually renewed = a daily multi-tenant
# outage LOOP. Restart ONLY when the DISK cert (Caddy's renewal) is strictly NEWER
# than the SERVED (in-memory) cert AND the pair validates. If disk is also stale
# (renewal pipeline broken), PAGE only — never thrash.
#
# Modes:
#   (default)         BOOTSTRAP: decide + restart. Honors ALARM_RESTART=0 (page-only).
#   --validate-only   parse the on-disk leaf pair, report valid/invalid, NEVER restart
#                     or probe — the safe command a CLIENT+REPAIR operator runs by hand.
#
# CLIENT+REPAIR (imagineering) installs NO timer and runs `--validate-only`; the
# runbook state machine owns the restart.
set -euo pipefail
cd "$(dirname "$0")"; . lib/cert-pair.sh
[ -f .env ] && set -a && . ./.env && set +a
: "${TURN_DOMAIN:?set TURN_DOMAIN}"
CERT_DIR="${CADDY_CERT_LEAF_DIR:?set CADDY_CERT_LEAF_DIR}"
: "${ALARM_RESTART:=1}"          # 0 => detector-only (page, never restart)
TURN_TLS_PORT="${TURN_TLS_PORT:-5349}"
HOST="${TURN_PROBE_HOST:-127.0.0.1}"
crt="$CERT_DIR/${TURN_DOMAIN}.crt"; key="$CERT_DIR/${TURN_DOMAIN}.key"

if [ "${1:-}" = "--validate-only" ]; then
  if cert_pair_matches "$crt" "$key" "$TURN_DOMAIN"; then echo "on-disk leaf pair VALID (matched, unexpired, for $TURN_DOMAIN)."; exit 0
  else echo "on-disk leaf pair INVALID (missing / mismatched / expired / wrong-domain) — do NOT restart onto it." >&2; exit 1; fi
elif [ -n "${1:-}" ]; then
  echo "unknown arg '$1' (only --validate-only). Failing closed." >&2; exit 2   # fail-closed on unknown flag
fi

set +e; ./served-cert-alarm.sh; rc=$?; set -e
case "$rc" in
  0)  echo "served cert healthy — no restart."; exit 0 ;;
  20) echo "endpoint DOWN — alarm paged; NOT restarting (not a renewal)."; exit 0 ;;
  10) echo "served cert STALE — checking whether disk has a NEWER renewed cert…" ;;
  *)  echo "alarm rc=$rc unexpected — failing closed, no restart." >&2; exit 1 ;;
esac

# Two-clock guard: only restart if the disk cert is a genuine renewal (strictly
# newer than what's served) AND the pair validates. Otherwise the renewal pipeline
# is broken — page (already done by the alarm) and STOP, don't thrash.
disk_epoch="$(cert_file_not_after_epoch "$crt" || true)"
served_epoch="$(served_cert_not_after_epoch "$HOST" "$TURN_TLS_PORT" "$TURN_DOMAIN" || true)"
if [ -z "$disk_epoch" ] || [ -z "$served_epoch" ]; then
  echo "could not read disk/served notAfter — failing closed, no restart." >&2; exit 1
fi
if [ "$disk_epoch" -le "$served_epoch" ]; then
  echo "disk cert is NOT newer than the served cert (renewal pipeline hasn't produced a fresh cert) — paged, NOT restarting (no thrash)." >&2; exit 0
fi
if ! cert_pair_matches "$crt" "$key" "$TURN_DOMAIN"; then
  echo "disk cert/key pair invalid (half-write / mismatch / expired / wrong-domain) — refusing restart (fail-closed)." >&2; exit 1
fi

if [ "$ALARM_RESTART" != "1" ]; then
  echo "ALARM_RESTART=$ALARM_RESTART (detector-only) — a fresh valid cert is on disk but restart is DISABLED; page and stop." >&2; exit 0
fi

echo "disk has a newer valid renewed pair — restarting livekit (served=$served_epoch disk=$disk_epoch)."
docker restart livekit
# Watermark (round-2 Tesla): require the SERVED cert to actually ADVANCE past what
# it was — a restart that comes back still serving the OLD in-memory cert (bad
# mount / wrong cert_file / partial config) would otherwise leave disk>served true
# forever and thrash a restart every tick. Measure the load, not the intent to load.
for i in $(seq 1 24); do
  now="$(served_cert_not_after_epoch "$HOST" "$TURN_TLS_PORT" "$TURN_DOMAIN" 2>/dev/null || true)"
  if [ -n "$now" ] && [ "$now" -gt "$served_epoch" ]; then
    echo "served cert advanced ($served_epoch -> $now) — renewal picked up."; exec ./served-cert-alarm.sh
  fi
  sleep 5
done
echo "FATAL: served cert did NOT advance past $served_epoch within 120s of restart — LiveKit is stuck serving the old in-memory cert (bad mount / cert_file / config). PAGE; NOT retrying restart until disk changes." >&2
./served-cert-alarm.sh || true
exit 1
