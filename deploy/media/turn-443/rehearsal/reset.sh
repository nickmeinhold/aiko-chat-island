#!/usr/bin/env bash
# reset.sh — return the rig to CP0 (pre-cutover) WITHOUT using rollback.sh.
#
# Deliberately independent of the artifact under test: if the reset between tests were
# rollback.sh, then a rollback bug would reset every run into the same wrong state and the
# whole matrix would agree with itself. This restores from the pristine copies provision.sh
# took, and then INDEPENDENTLY asserts the baseline before any test is allowed to start.
set -uo pipefail
TURN_DOMAIN="${TURN_DOMAIN:-turn.enspyr.co}"
LIVEKIT_DIR=/home/ubuntu/apps/livekit
P=/opt/rehearsal-pristine
log() { echo "[reset] $*"; }

[ "$(id -u)" -eq 0 ] || { echo "[reset] must be root"; exit 1; }
[ -s "$P/Caddyfile" ] && [ -s "$P/livekit.yaml" ] || { echo "[reset] pristine copies missing — re-run provision.sh"; exit 1; }

# 1. HAProxy off and OFF the boot path (it must not own :443 at CP0).
systemctl disable --now haproxy >/dev/null 2>&1 || true

# 2. Configs back to pristine.
cp "$P/Caddyfile"    /etc/caddy/Caddyfile
cp "$P/livekit.yaml" "${LIVEKIT_DIR}/livekit.yaml"
rm -f /etc/caddy/Caddyfile.stock "${LIVEKIT_DIR}/livekit.yaml.stock"

# 3. Firewall: drop every guard rule this rehearsal may have added, both families. Loop
#    because a rule can legitimately be present more than once after a partial-apply test.
for fam in iptables ip6tables; do
  for port in 5349 8443; do
    for _ in $(seq 1 10); do
      "$fam" -C INPUT ! -i lo -p tcp --dport "$port" -j DROP 2>/dev/null || break
      "$fam" -D INPUT ! -i lo -p tcp --dport "$port" -j DROP 2>/dev/null || break
    done
  done
done
netfilter-persistent save >/dev/null 2>&1 || true

# 4. Services back up in the CP0 shape.
systemctl restart caddy
(cd "$LIVEKIT_DIR" && docker compose restart livekit >/dev/null 2>&1 || docker compose up -d >/dev/null 2>&1)

# 5. Independent baseline assertion — a reset that silently half-worked would poison the run.
ok=1
for _ in $(seq 1 30); do
  [ "$(ss -tlnpH 'sport = :443' 2>/dev/null | grep -c caddy)" -ge 1 ] \
    && [ "$(docker inspect livekit --format '{{.State.Running}}' 2>/dev/null)" = "true" ] \
    && curl -sS --max-time 5 -o /dev/null "https://chat.${TURN_DOMAIN#turn.}/" 2>/dev/null \
    && { ok=0; break; }
  sleep 1
done
if [ $ok -ne 0 ]; then
  log "BASELINE NOT RESTORED — refusing to hand a poisoned rig to the next test"
  ss -tlnp 2>/dev/null | grep -E ':(443|5349|8443)\b'
  exit 1
fi
grep -q 'external_tls' "${LIVEKIT_DIR}/livekit.yaml" && { log "BASELINE BAD: livekit.yaml still has external_tls"; exit 1; }
log "CP0 baseline restored + verified"
