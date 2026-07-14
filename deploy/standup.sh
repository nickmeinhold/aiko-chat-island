#!/usr/bin/env bash
#
# standup.sh — stand up a brand-new aiko island on a fresh host.
#
# An "island" is a complete, self-contained aiko mesh: 4 containers (the gateway
# from this repo + its own mosquitto broker, registrar, and ChatServer) from ONE
# published image plus stock mosquitto. This script:
#
#   1. creates the `external` `aiko_data` volume (the SQLite store's stable home),
#   2. writes a production `.env` with a freshly-generated strong JWT secret and
#      this island's identity (domain, display name, optional federation peers),
#   3. PULLS the published image and starts the stack (no build — the gateway image
#      serves all three aiko roles by command override; `--from-source` builds it
#      from this checkout instead),
#   4. brings up Caddy for HTTPS (skippable) and verifies.
#
# Design goal (from docker-compose.yml): "one script, and it just works." Safe to
# re-run — it never rotates an existing JWT secret and skips work already done.
#
# Usage:
#   deploy/standup.sh --domain chat.example.org --name "Example Island"
#   deploy/standup.sh --domain chat.example.org --name "Example Island" \
#       --seed-peers '[{"id":"chat.imagineering.cc","display_name":"Aiko","base_url":"https://chat.imagineering.cc"}]'
#   deploy/standup.sh --domain chat.example.org --name "Example Island" --no-tls
#
# Flags (all optional except --domain and --name, which prompt if omitted):
#   --domain <host>       public hostname for this island (DNS A record -> this host)
#   --name "<label>"      human label the app's island picker shows
#   --seed-peers <json>   JSON array of {"id","display_name","base_url"} to federate with
#   --enable-passkeys     advertise passkey sign-in (only after well-known files serve; see guide)
#   --no-tls              skip the bundled Caddy step (you run your own reverse proxy)
#   --from-source         build the island image from this checkout instead of pulling
#   --yes                 non-interactive; fail instead of prompting for missing values

set -euo pipefail

# --- pretty logging --------------------------------------------------------
c_bold=$'\033[1m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_red=$'\033[31m'; c_rst=$'\033[0m'
log()  { printf '%s==>%s %s\n'  "$c_bold" "$c_rst" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$c_grn"  "$c_rst" "$*"; }
warn() { printf '%s warn%s %s\n' "$c_ylw" "$c_rst" "$*" >&2; }
die()  { printf '%s fail%s %s\n' "$c_red" "$c_rst" "$*" >&2; exit 1; }

# --- locate repo root (this script lives in deploy/) ------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
[ -f docker-compose.yml ] || die "docker-compose.yml not found in $REPO_ROOT — run from the aiko-chat-island checkout"

# --- defaults + arg parsing -------------------------------------------------
DOMAIN=""; DISPLAY_NAME=""; SEED_PEERS="[]"; ENABLE_PASSKEYS="false"; DO_TLS="true"; INTERACTIVE="true"; FROM_SOURCE="false"
DATA_VOLUME="aiko_data"

while [ $# -gt 0 ]; do
  case "$1" in
    --domain)         DOMAIN="${2:-}"; shift 2 ;;
    --name)           DISPLAY_NAME="${2:-}"; shift 2 ;;
    --seed-peers)     SEED_PEERS="${2:-[]}"; shift 2 ;;
    --enable-passkeys) ENABLE_PASSKEYS="true"; shift ;;
    --no-tls)         DO_TLS="false"; shift ;;
    --from-source)    FROM_SOURCE="true"; shift ;;
    --yes)            INTERACTIVE="false"; shift ;;
    -h|--help)        sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;/^set -euo/d'; exit 0 ;;
    *)                die "unknown argument: $1 (see --help)" ;;
  esac
done

prompt() { # prompt VAR "question" — only when interactive and value empty
  local __var="$1" __q="$2" __ans=""
  [ -n "${!__var}" ] && return 0
  [ "$INTERACTIVE" = "true" ] || die "$__var is required (non-interactive mode; pass the matching flag)"
  read -r -p "$__q " __ans
  printf -v "$__var" '%s' "$__ans"
}

prompt DOMAIN       "Island domain (public hostname, e.g. chat.example.org):"
prompt DISPLAY_NAME "Display name (label shown in the app's island picker):"
[ -n "$DOMAIN" ]       || die "domain is required"
[ -n "$DISPLAY_NAME" ] || die "display name is required"
# Guard against a scheme being pasted in — we want a bare host.
case "$DOMAIN" in *"/"*) die "domain must be a bare hostname (no https:// or path): $DOMAIN" ;; esac

BASE_URL="https://$DOMAIN"

# --- preflight: required tools ---------------------------------------------
log "Preflight — checking required tools"
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
need docker; need openssl; need curl
docker compose version >/dev/null 2>&1 || die "docker compose v2 not available (need the 'docker compose' plugin)"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running? are you in the docker group?)"
ok "docker, git, openssl, curl, docker compose present"

# --- preflight: TLS preconditions (only when we run the bundled Caddy) -------
# Both checks below matter ONLY under the bundled Caddy; --no-tls means the
# operator brings their own proxy, so neither DNS-points-here nor ports-80/443-free
# is our concern.
if [ "$DO_TLS" = "true" ]; then
  # DNS advisory (does the domain point here?)
  log "Preflight — DNS advisory for $DOMAIN"
  host_ip="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
  dom_ip="$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)"
  if [ -n "$host_ip" ] && [ -n "$dom_ip" ]; then
    if [ "$host_ip" = "$dom_ip" ]; then
      ok "$DOMAIN resolves to this host ($host_ip)"
    else
      warn "$DOMAIN resolves to $dom_ip but this host's public IP is $host_ip."
      warn "TLS issuance will fail until the A record points here. Continuing (fix DNS before relying on HTTPS)."
    fi
  else
    warn "Could not confirm DNS (resolved='$dom_ip' host_ip='$host_ip'). Ensure $DOMAIN -> this host before trusting HTTPS."
  fi

  # Port advisory. The bundled Caddy uses network_mode: host and binds 80+443 for
  # ACME — but with host networking docker can't detect the clash at create time,
  # so a pre-existing Caddy/nginx/Apache lets `up -d` report success while Caddy
  # crash-loops, surfacing only as an opaque "https not answering" 30s later. Catch
  # it here and name the escape (--no-tls). SKIP when OUR OWN Caddy from a prior run
  # holds the ports (idempotent re-run must not self-abort).
  ours_caddy="$(docker compose -f "$SCRIPT_DIR/caddy/docker-compose.caddy.yml" ps -q caddy 2>/dev/null || true)"
  if [ -z "$ours_caddy" ]; then
    if command -v ss >/dev/null 2>&1; then
      if ss -ltnH 2>/dev/null | awk '{n=split($4,a,":"); print a[n]}' | grep -qxE '80|443'; then
        die "port 80 and/or 443 is already in use (an existing Caddy/nginx/Apache?).
     The bundled Caddy binds them for Let's Encrypt and would crash-loop. Either stop
     the other proxy, or re-run with --no-tls and point your existing proxy at
     127.0.0.1:8095 (the gateway's local publish)."
      fi
      ok "ports 80 + 443 are free for the bundled Caddy"
    else
      warn "could not check ports 80/443 (no 'ss' on PATH); if another proxy is running, re-run with --no-tls."
    fi
  fi
fi

# --- step 1: external data volume ------------------------------------------
log "Step 1/4 — persistent data volume ($DATA_VOLUME)"
if docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1; then
  ok "volume $DATA_VOLUME already exists — leaving it (never re-created; it holds the DB)"
else
  docker volume create "$DATA_VOLUME" >/dev/null
  ok "created external volume $DATA_VOLUME"
fi

# --- step 2: write the production .env --------------------------------------
log "Step 2/4 — writing .env (island identity + secrets)"
ENV_FILE="$REPO_ROOT/.env"

# Preserve an existing JWT secret across re-runs — rotating it invalidates every
# live session. Only mint a new one on the very first run.
existing_secret=""
if [ -f "$ENV_FILE" ]; then
  existing_secret="$(grep -E '^JWT_SECRET=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
fi
if [ -n "$existing_secret" ] && [ "${#existing_secret}" -ge 32 ]; then
  JWT_SECRET="$existing_secret"
  ok "reusing existing JWT_SECRET from .env (not rotated)"
else
  JWT_SECRET="$(openssl rand -hex 32)"   # 64 hex chars — comfortably over the 32-char floor
  ok "generated a fresh 64-char JWT_SECRET"
fi

# NOTE: no ENVIRONMENT line — absence means production, which arms the fail-closed
# JWT guard. Setting ENVIRONMENT=dev here would DISABLE that guard. Never do it.
umask 077   # .env holds the JWT secret — owner-only
cat > "$ENV_FILE" <<EOF
# Generated by deploy/standup.sh for island: $DOMAIN
# Production config. ENVIRONMENT is intentionally UNSET (absence => production =>
# fail-closed JWT guard armed). Do NOT add ENVIRONMENT=dev here.

JWT_SECRET=$JWT_SECRET

# --- island identity (this compose is the island template) ---
GATEWAY_BASE_URL=$BASE_URL
GATEWAY_DISPLAY_NAME=$DISPLAY_NAME
PASSKEY_RP_ID=$DOMAIN

# Federation: operator-curated peers this island advertises in its directory.
# JSON array of {"id","display_name","base_url"}. Empty [] = solo island.
GATEWAY_SEED_PEERS=$SEED_PEERS

# Passkeys: advertise passkey sign-in via /v1/auth/providers. Leave false until
# this island serves valid /.well-known assetlinks.json + AASA for its domain
# (see docs/standup-guide.md) — advertising before that dies mid-ceremony.
PASSKEY_ENABLED=$ENABLE_PASSKEYS
EOF
chmod 600 "$ENV_FILE"
ok "wrote $ENV_FILE (mode 600)"

# --- step 3: bring the island up (4 containers, 1 image + stock mosquitto) ---
if [ "$FROM_SOURCE" = "true" ]; then
  log "Step 3/4 — building the island image from source + starting (gateway + broker + registrar + ChatServer)"
  docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
else
  log "Step 3/4 — pulling the published island image + starting (gateway + broker + registrar + ChatServer)"
  docker compose pull
  docker compose up -d
fi
ok "compose stack started"

log "waiting for the gateway to pass its health check (migrate-then-serve)…"
health_ok="false"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8095/health >/dev/null 2>&1; then
    health_ok="true"; break
  fi
  sleep 2
done
if [ "$health_ok" = "true" ]; then
  ok "gateway healthy on 127.0.0.1:8095 (schema migrated to head)"
else
  warn "gateway did not answer /health within ~60s. Inspect: docker compose logs chat-island"
fi

# --- step 5: TLS via Caddy (optional) --------------------------------------
if [ "$DO_TLS" = "true" ]; then
  log "Step 4/4 — TLS reverse proxy (Caddy, host network)"
  caddy_env="$SCRIPT_DIR/caddy/.env"
  printf 'ISLAND_DOMAIN=%s\n' "$DOMAIN" > "$caddy_env"
  docker compose -f "$SCRIPT_DIR/caddy/docker-compose.caddy.yml" up -d
  ok "Caddy up — it will obtain a Let's Encrypt cert for $DOMAIN (needs ports 80+443 open)"
  log "verifying HTTPS end-to-end (allow up to ~30s for cert issuance)…"
  https_ok="false"
  for _ in $(seq 1 15); do
    if curl -fsS --max-time 4 "https://$DOMAIN/health" >/dev/null 2>&1; then
      https_ok="true"; break
    fi
    sleep 2
  done
  if [ "$https_ok" = "true" ]; then
    ok "https://$DOMAIN/health is live 🎉"
  else
    warn "https://$DOMAIN/health not answering yet. Check: DNS points here, ports 80+443 open, then: docker compose -f deploy/caddy/docker-compose.caddy.yml logs"
  fi
else
  log "Step 4/4 — TLS skipped (--no-tls). Point your own proxy at 127.0.0.1:8095."
fi

# --- done -------------------------------------------------------------------
echo
log "${c_bold}Island '$DISPLAY_NAME' ($DOMAIN) is up.${c_rst}"
cat <<EOF

Next steps:
  • Verify:        curl -s https://$DOMAIN/health | jq
  • Directory:     curl -s https://$DOMAIN/v1/islands | jq   (this island + any seed peers)
  • Federate:      re-run with --seed-peers '[…]' to list other islands, or ask them to add yours.
  • Passkeys:      serve /.well-known files for $DOMAIN, then re-run with --enable-passkeys.
                   See docs/standup-guide.md § Passkeys.
  • Backups:       the SQLite store lives in volume '$DATA_VOLUME'. Set up a backup
                   before real users arrive — see docs/deploy-passkeys-runbook.md.

Re-running this script is safe: it won't rotate your JWT secret or wipe data.
EOF
