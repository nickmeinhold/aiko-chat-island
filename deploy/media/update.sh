#!/usr/bin/env bash
# update.sh — pull the pinned LiveKit image and roll it, backup-first, verify.
# Pin bumps are a TURN-auth BEHAVIOR CHANGE (DESIGN §3.3): edit the tag in
# docker-compose.yml, run this, then RE-RUN the acceptance gates. Rollback = revert
# the tag and re-run.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
: "${TURN_DOMAIN:?}"

PINNED="$(grep -oE 'livekit/livekit-server:[^[:space:]]+' docker-compose.yml | head -1)"
echo "pinned image: $PINNED"
echo "current running: $(docker inspect livekit --format '{{.Config.Image}}' 2>/dev/null || echo none)"

echo "== pull pinned =="
docker compose pull

echo "== roll =="
docker compose up -d

echo "== verify: served-cert endpoint + relay gate A =="
sleep 5
# The alarm's rc=10 means "endpoint UP, cert aging" — that is renewal debt, NOT an
# update failure (a pin-roll inside the alarm window would else always false-red).
# Only endpoint-DOWN (20) or an unexpected rc fails the update (round-2 Tesla).
set +e; ./served-cert-alarm.sh; arc=$?; set -e
case "$arc" in
  0|10) echo "endpoint up (alarm rc=$arc$( [ "$arc" = 10 ] && echo ': cert aging, not an update failure' ))." ;;
  *)    echo "served-cert endpoint DOWN/unexpected after update (rc=$arc) — roll back the tag." >&2; exit 1 ;;
esac
python3 e2e_media_relay.py --host "$TURN_DOMAIN" || {
  echo "relay gate A FAILED after pin — a v1.12/v1.13.1 TURN-auth change may have broken relay. Roll back the tag." >&2; exit 1; }
echo "update verified."
