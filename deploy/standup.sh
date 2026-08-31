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
# re-run: it never rotates an existing JWT secret, skips work already done, and
# PRESERVES the operator choices already recorded in .env (federation peers, passkey
# advertisement) unless a flag overrides them. Before #3734 that last clause was not
# true, and the failure was silent — a re-run without --seed-peers emptied the
# federation link while every health check stayed green.
#
# Usage:
#   deploy/standup.sh --domain chat.example.org --name "Example Island"
#   deploy/standup.sh --domain chat.example.org --name "Example Island" \
#       --seed-peers '[{"id":"chat.imagineering.cc","display_name":"Aiko","base_url":"https://chat.imagineering.cc"}]'
#   deploy/standup.sh --domain chat.example.org --name "Example Island" --no-tls
#   deploy/standup.sh --domain chat.example.org --name "Example Island" \
#       --with-media --turn-domain turn.example.org
#
# Flags (all optional except --domain and --name, which prompt if omitted):
#   --domain <host>       public hostname for this island (DNS A record -> this host)
#   --name "<label>"      human label the app's island picker shows
#   --seed-peers <json>   JSON array of {"id","display_name","base_url"} to federate with
#   --enable-passkeys     advertise passkey sign-in (only after well-known files serve; see guide)
#   --no-passkeys         stop advertising passkey sign-in. Passing NEITHER flag keeps
#                         whatever this island already recorded — absence is not "off".
#   --no-tls              skip the bundled Caddy step (you run your own reverse proxy)
#   --with-media          also stand up a bundled LiveKit SFU for calls (OFF by default;
#                         without it the island has no video and the token endpoint 503s,
#                         which is a supported state — or point LIVEKIT_URL at your own SFU)
#   --turn-domain <host>  public hostname for TURN when --with-media (default: turn.<domain>)
#   --livekit-domain <host>  public hostname for the SFU WEBSOCKET when --with-media
#                         (default: livekit.<domain>). SEPARATE from --turn-domain:
#                         both live islands serve the SFU on livekit.<host> and TURN on
#                         turn.<host>, and handing clients the TURN name as LIVEKIT_URL
#                         points them at the wrong endpoint.
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
# SEED_PEERS and ENABLE_PASSKEYS default to EMPTY, not to their conventional values.
# Empty means "the operator said nothing this run" and must consult the recorded
# value (deploy/resolve-gateway-env.sh); "[]" / "false" are real choices that win.
# Collapsing those two is #3734 — a re-run silently dropped the federation link.
DOMAIN=""; DISPLAY_NAME=""; SEED_PEERS=""; ENABLE_PASSKEYS=""; DO_TLS="true"; INTERACTIVE="true"; FROM_SOURCE="false"
# Media is OFF by default, unlike TLS. An island without HTTPS is broken; an island
# without an SFU is a supported configuration the code already models
# (livekit_tokens.is_configured() -> 503 "capability disabled"). Defaulting it ON
# would also fail preflight for most new islands, which need a turn.<host> DNS
# record and open UDP ranges before an SFU can do anything.
DO_MEDIA="false"; TURN_DOMAIN=""; LIVEKIT_DOMAIN=""
DATA_VOLUME="aiko_data"

while [ $# -gt 0 ]; do
  case "$1" in
    --domain)         DOMAIN="${2:-}"; shift 2 ;;
    --name)           DISPLAY_NAME="${2:-}"; shift 2 ;;
    # ${2:?} not ${2:-[]}: a flag with no value is a MALFORMED COMMAND, not a
    # request to erase the peer list. Going solo stays expressible as an
    # explicit --seed-peers '[]'. Matches --turn-domain/--livekit-domain.
    # tr: the guide documents a MULTILINE JSON array, and a newline-bearing
    # value cannot survive .env (line-oriented) OR the resolver protocol —
    # it truncated to "[". Newlines are insignificant whitespace outside a
    # JSON string (a literal newline inside one must be escaped), so
    # collapsing them is safe and makes the documented invocation work.
    --seed-peers)     SEED_PEERS="$(printf '%s' "${2:?--seed-peers needs a value}" | tr '\n' ' ')"; shift 2 ;;
    --enable-passkeys) ENABLE_PASSKEYS="true"; shift ;;
    --no-passkeys)    ENABLE_PASSKEYS="false"; shift ;;
    --no-tls)         DO_TLS="false"; shift ;;
    --with-media)     DO_MEDIA="true"; shift ;;
    --turn-domain)    TURN_DOMAIN="${2:?--turn-domain needs a value}"; shift 2 ;;
    --livekit-domain) LIVEKIT_DOMAIN="${2:?--livekit-domain needs a value}"; shift 2 ;;
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
# --- preflight: media preconditions (only when we run the bundled SFU) -------
# Same discipline as the TLS block: these matter ONLY under --with-media. An
# island pointing LIVEKIT_URL at someone else's SFU owes none of it.
lk_env="$SCRIPT_DIR/livekit/.env"
_read_kv() { [ -f "$1" ] && grep -E "^$2=" "$1" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }

# Hostname order and credential-source choice live in deploy/resolve-media-env.sh,
# which is TESTED (tests/test_resolve_media_env.py, both controls, mutation-proven).
# They used to be inlined here, and every finding across three cage-match rounds on
# PR#151 was in this wiring — including round 1's hostname bug reappearing inside
# round 3's fix, because nothing here could go red. Calling the tested script means
# the code under test IS the code that runs; a second copy would be worse than none.
#
# It runs unconditionally, not just under --with-media: the no---with-media re-run
# needs the same credential decision to carry an existing pair forward.
# ONE invocation, and stderr is NOT captured — the refusal messages name which file is
# damaged and what to do, so they belong on the operator's terminal, not swallowed into
# a variable this script would have to re-print.
set +e
_media_env="$("$SCRIPT_DIR/resolve-media-env.sh" \
    "$REPO_ROOT/.env" "$lk_env" "$DOMAIN" "$TURN_DOMAIN" "$LIVEKIT_DOMAIN")"
_media_rc=$?
set -e
[ "$_media_rc" -eq 0 ] || die "media environment could not be resolved — see the refusal above."
TURN_DOMAIN="$(printf '%s\n' "$_media_env" | grep '^TURN_DOMAIN=' | cut -d= -f2-)"
LIVEKIT_DOMAIN="$(printf '%s\n' "$_media_env" | grep '^LIVEKIT_DOMAIN=' | cut -d= -f2-)"
CRED_SOURCE="$(printf '%s\n' "$_media_env" | grep '^CRED_SOURCE=' | cut -d= -f2-)"

if [ "$DO_MEDIA" = "true" ]; then
  log "Preflight — media (SFU on $LIVEKIT_DOMAIN, TURN on $TURN_DOMAIN)"

  # The collision this whole overlay exists to avoid: a LiveKit already running
  # on this host. With network_mode: host, docker CANNOT detect the clash at
  # create time — `up -d` reports success while the new server crash-loops on
  # bind, and it surfaces only as "calls don't connect". imagineering runs
  # exactly this shape (an SFU shared with another product), which is why the
  # bundled arm is opt-in rather than default.
  ours_lk="$(docker compose -f "$SCRIPT_DIR/livekit/docker-compose.livekit.yml" ps -q --status running livekit 2>/dev/null || true)"
  if [ -z "$ours_lk" ]; then
    # FAIL CLOSED when we cannot look (cage-match PR#151, Carnot): with neither ss
    # nor lsof the old loop reported success, which defeats the whole preflight for
    # a host-network service whose failure mode is crash-loop-AFTER-success.
    if ! command -v ss >/dev/null 2>&1 && ! command -v lsof >/dev/null 2>&1; then
      die "neither 'ss' nor 'lsof' is available, so the media port check cannot run.
     Refusing --with-media rather than reporting a clean preflight we did not perform.
     Install iproute2 (ss) or lsof, or use an existing SFU via LIVEKIT_URL."
    fi
    for p in 7880 7881 3478; do
      # Isolate the port FIELD then match it exactly — the same idiom this script
      # already uses for 80/443 below, rather than a second weaker one grepping the
      # raw line (cage-match PR#151, Kelvin: the repo's own pattern was better).
      # -lntu covers UDP, so TURN's 3478 is actually checked; the lsof fallback adds
      # -iUDP for the same reason (an -iTCP-only check misses a UDP TURN collision).
      if (command -v ss >/dev/null 2>&1 \
            && ss -lntuH 2>/dev/null | awk '{n=split($5,a,":"); print a[n]}' | grep -qxF "$p") \
      || (command -v lsof >/dev/null 2>&1 \
            && lsof -nP -iTCP:"$p" -sTCP:LISTEN -t >/dev/null 2>&1) \
      || (command -v lsof >/dev/null 2>&1 \
            && lsof -nP -iUDP:"$p" -t >/dev/null 2>&1); then
        die "port $p is already in use on this host — something is already serving media here.
     If that is an SFU you already run, do NOT pass --with-media: set LIVEKIT_URL,
     LIVEKIT_API_KEY and LIVEKIT_API_SECRET in the island's .env to point at it."
      fi
    done
    ok "media ports 7880/7881/3478 are free"
  else
    ok "our own bundled SFU already holds the ports (re-run is idempotent)"
  fi

  # DNS advisory for TURN. Not fatal: certs and DNS often land after standup.
  turn_ip="$(getent hosts "$TURN_DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)"
  if [ -n "$turn_ip" ]; then
    ok "$TURN_DOMAIN resolves to $turn_ip"
  else
    warn "$TURN_DOMAIN does not resolve yet. Relay will fail until it points at this host."
  fi
fi

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
# Step count depends on whether the optional media step runs, so the labels stay
# honest in both modes rather than saying "4/4" while a fifth step follows.
TOTAL=4; [ "$DO_MEDIA" = "true" ] && TOTAL=5
log "Step 1/$TOTAL — persistent data volume ($DATA_VOLUME)"
if docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1; then
  ok "volume $DATA_VOLUME already exists — leaving it (never re-created; it holds the DB)"
else
  docker volume create "$DATA_VOLUME" >/dev/null
  ok "created external volume $DATA_VOLUME"
fi

# --- step 2: write the production .env --------------------------------------
# --- media credentials, resolved BEFORE the .env write ---------------------
# The SFU and the gateway must hold the SAME pair: the gateway mints join tokens the
# SFU has to accept. The gateway .env is written wholesale below, so this is decided
# here rather than patched in afterwards.
#
# Two re-run hazards, both found by the cage-match on PR#151 (Kelvin + Carnot, both
# rated ship-blocking) and both fixed here rather than documented:
#
#   1. OMITTING --with-media on a re-run of an island that HAS media used to leave
#      LIVEKIT_ENV_BLOCK empty, and the wholesale .env rewrite then SILENTLY STRIPPED
#      LIVEKIT_*. The SFU kept running while the gateway went 503 — a torn media plane
#      produced by a command advertised as safe to re-run. So credentials already in
#      the gateway .env are now CARRIED FORWARD whether or not the flag is passed.
#   2. Losing deploy/livekit/.env while the gateway .env survived used to mint a FRESH
#      pair, rotating a live media secret. The pair is now recovered from EITHER file,
#      and a conflict between two non-empty pairs FAILS CLOSED rather than silently
#      picking one — a wrong pick is a media plane that authenticates nothing.
LIVEKIT_ENV_BLOCK=""
# lk_env and _read_kv are defined above the media preflight, which needs them to
# recover the recorded hostnames before defaulting by convention.

# The pair's SOURCE was decided by resolve-media-env.sh in the preflight above, which
# is tested with both controls and mutation-proven. Every refusal case — a half-present
# pair on either file, two complete pairs that disagree — has already aborted there, so
# all that is left here is to read the pair from the file that won.
#
# This block used to re-implement that state machine inline, untested. Deleting the copy
# is the point of the extraction: a test that exercises a parallel implementation is
# worse than no test, because it reports green about code nobody runs.
gw_url="$(_read_kv "$REPO_ROOT/.env" LIVEKIT_URL)"
case "$CRED_SOURCE" in
  sfu)
    lk_key="$(_read_kv "$lk_env" LIVEKIT_API_KEY_ID)"
    lk_secret="$(_read_kv "$lk_env" LIVEKIT_API_SECRET)" ;;
  gateway)
    lk_key="$(_read_kv "$REPO_ROOT/.env" LIVEKIT_API_KEY)"
    lk_secret="$(_read_kv "$REPO_ROOT/.env" LIVEKIT_API_SECRET)" ;;
  *)
    lk_key=""; lk_secret="" ;;
esac

if [ "$DO_MEDIA" = "true" ]; then
  if [ -n "$lk_key" ] && [ -n "$lk_secret" ]; then
    ok "reusing the existing SFU key pair (a re-run never rotates a live media secret)"
  else
    lk_key="API$(openssl rand -hex 8)"
    lk_secret="$(openssl rand -base64 48 | tr -d '\n')"
    ok "generated a fresh SFU key pair for this island"
  fi

  node_ip="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
  [ -n "$node_ip" ] || die "could not determine this host's public IP for node_ip.
     A blank node_ip advertises an unreachable ICE candidate and every call fails at
     CONNECT, silently. Set LIVEKIT_NODE_IP by hand in $lk_env and re-run."

  # Write to a FRESH file then rename, never `cat >` an existing one: redirection
  # truncates and writes into the EXISTING inode, so a .env already sitting at 0644
  # from a manual copy holds the new secret world-readable until the chmod lands.
  # umask only governs files it CREATES (cage-match PR#151 round 3, Carnot).
  umask 077
  lk_env_tmp="$(mktemp "${lk_env}.XXXXXX")"
  cat > "$lk_env_tmp" <<EOF_LK
# Generated by deploy/standup.sh — per-island values for the bundled SFU.
LIVEKIT_NODE_IP=$node_ip
LIVEKIT_TURN_DOMAIN=$TURN_DOMAIN
# Persisted so a later recovery reads the hostname this island ACTUALLY uses
# rather than re-deriving livekit.<domain> by convention — an operator who passed
# --livekit-domain would otherwise have it silently replaced on recovery, sending
# clients to the wrong websocket host (cage-match PR#151 round 3, Carnot).
LIVEKIT_DOMAIN=$LIVEKIT_DOMAIN
LIVEKIT_API_KEY_ID=$lk_key
LIVEKIT_API_SECRET=$lk_secret
EOF_LK
  chmod 600 "$lk_env_tmp"
  mv "$lk_env_tmp" "$lk_env"
  ok "wrote $lk_env (mode 600, via atomic rename)"

  LIVEKIT_ENV_BLOCK="
# --- media (bundled SFU, --with-media). MUST match deploy/livekit/.env. ---
LIVEKIT_URL=wss://$LIVEKIT_DOMAIN
LIVEKIT_API_KEY=$lk_key
LIVEKIT_API_SECRET=$lk_secret"

elif [ -n "$lk_key" ]; then
  # Media NOT requested this run, but this island already has a credential somewhere.
  # Carry it forward — including a BYO LIVEKIT_URL pointing at someone else's SFU.
  #
  # The branch keys on $lk_key — the pair from WHICHEVER file the resolver chose — not
  # on the gateway .env specifically. An earlier fix covered only the case where the
  # GATEWAY .env survived. If the gateway .env was deleted while
  # deploy/livekit/.env survived, that version wrote a fresh .env with no media lines
  # and left a running SFU beside a 503ing gateway — the same torn media plane by the
  # other route (cage-match PR#151 round 2, Carnot). Recovering from either side
  # closes both directions.
  if [ -n "$gw_url" ]; then
    lk_url="$gw_url"
  else
    # Gateway .env gone: reconstruct the SFU URL from the bundled config's own TURN
    # domain, which is the only surviving statement of this island's media hostnames.
    # Prefer the hostname the bundled SFU RECORDED; fall back to convention only
    # when nothing recorded one (a BYO island whose gateway .env was deleted).
    lk_dom="$(_read_kv "$lk_env" LIVEKIT_DOMAIN)"
    if [ -n "$lk_dom" ]; then
      lk_url="wss://$lk_dom"
      ok "recovered LIVEKIT_URL from the bundled SFU env ($lk_url)"
    else
      lk_url="wss://${LIVEKIT_DOMAIN:-livekit.$DOMAIN}"
      warn "no LIVEKIT_URL or LIVEKIT_DOMAIN was recorded anywhere; guessed $lk_url by convention. VERIFY it before relying on calls."
    fi
  fi
  LIVEKIT_ENV_BLOCK="
# --- media: preserved across a re-run (not re-run with --with-media) ---
LIVEKIT_URL=$lk_url
LIVEKIT_API_KEY=$lk_key
LIVEKIT_API_SECRET=$lk_secret"
  ok "preserved this island's LIVEKIT_* credentials (media untouched this run)"
fi

log "Step 2/$TOTAL — writing .env (island identity + secrets)"
ENV_FILE="$REPO_ROOT/.env"

# Preserve an existing JWT secret across re-runs — rotating it invalidates every
# live session. Only mint a new one on the very first run.
existing_secret=""
if [ -f "$ENV_FILE" ]; then
  existing_secret="$(grep -E '^JWT_SECRET=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
fi
# Resolve the operator CHOICES that survive a re-run. Must happen BEFORE the heredoc
# below rewrites $ENV_FILE, since the record it reads is that same file (#3734).
# FAIL CLOSED. `done < <(cmd)` swallows cmd's exit status even under
# `set -euo pipefail` — the loop simply reads nothing, both variables stay empty,
# and the heredoc below then writes `GATEWAY_SEED_PEERS=` … which compose reads
# through `${GATEWAY_SEED_PEERS:-[]}` as `[]`. That is #3734 itself, reintroduced
# through the ERROR PATH of the code that exists to prevent it. So: capture to a
# file, CHECK the status, and die rather than write unresolved choices.
RESOLVED_ENV="$(mktemp "${TMPDIR:-/tmp}/aiko-resolved.XXXXXX")"
if ! "$SCRIPT_DIR/resolve-gateway-env.sh" "$ENV_FILE" "$SEED_PEERS" "$ENABLE_PASSKEYS" > "$RESOLVED_ENV"; then
  rm -f "$RESOLVED_ENV"
  die "resolve-gateway-env.sh failed — refusing to write .env with unresolved operator choices"
fi
# Prefix-strip rather than IFS='=' split: read strips TRAILING IFS characters, which
# would silently truncate any value ending in '='.
while read -r _line; do
  case "$_line" in
    SEED_PEERS=*)      SEED_PEERS="${_line#SEED_PEERS=}" ;;
    PASSKEY_ENABLED=*) ENABLE_PASSKEYS="${_line#PASSKEY_ENABLED=}" ;;
  esac
done < "$RESOLVED_ENV"
rm -f "$RESOLVED_ENV"

if [ -n "$existing_secret" ] && [ "${#existing_secret}" -ge 32 ]; then
  JWT_SECRET="$existing_secret"
  ok "reusing existing JWT_SECRET from .env (not rotated)"
else
  JWT_SECRET="$(openssl rand -hex 32)"   # 64 hex chars — comfortably over the 32-char floor
  ok "generated a fresh 64-char JWT_SECRET"
fi

# NOTE: no ENVIRONMENT line — absence means production, which arms the fail-closed
# JWT guard. Setting ENVIRONMENT=dev here would DISABLE that guard. Never do it.
# Same fresh-file-then-rename discipline as the SFU env above: this file holds
# JWT_SECRET and (when media is on) the LiveKit secret, and `cat >` on a
# pre-existing 0644 .env would expose both until the chmod.
umask 077   # .env holds the JWT secret — owner-only
ENV_TMP="$(mktemp "${ENV_FILE}.XXXXXX")"
cat > "$ENV_TMP" <<EOF
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
$LIVEKIT_ENV_BLOCK
EOF
chmod 600 "$ENV_TMP"
mv "$ENV_TMP" "$ENV_FILE"
ok "wrote $ENV_FILE (mode 600, via atomic rename)"

# --- step 3: bring the island up (4 containers, 1 image + stock mosquitto) ---
if [ "$FROM_SOURCE" = "true" ]; then
  log "Step 3/$TOTAL — building the island image from source + starting (gateway + broker + registrar + ChatServer)"
  docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
else
  log "Step 3/$TOTAL — pulling the published island image + starting (gateway + broker + registrar + ChatServer)"
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
  log "Step 4/$TOTAL — TLS reverse proxy (Caddy, host network)"
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
  log "Step 4/$TOTAL — TLS skipped (--no-tls). Point your own proxy at 127.0.0.1:8095."
fi

# --- step 5: bundled LiveKit SFU (optional, OFF by default) -----------------
# The key pair and both .env files were resolved before step 2 (the gateway .env
# is written wholesale there, so the pair has to exist first). All that is left
# here is rendering the config and bringing the SFU up.
if [ "$DO_MEDIA" = "true" ]; then
  log "Step 5/$TOTAL — bundled LiveKit SFU (host network)"
  lk_dir="$SCRIPT_DIR/livekit"
  "$lk_dir/render-config.sh"

  # --force-recreate is load-bearing, not belt-and-braces. livekit.yaml is a BIND
  # MOUNT, so re-rendering it changes no compose spec — a plain `up -d` sees an
  # up-to-date container and leaves the old one running with the OLD keys, while the
  # gateway has just been written the new ones. Every join would then be rejected by
  # a healthy-looking SFU. LiveKit reads its config only at boot, so the recreate IS
  # the mechanism by which a re-render takes effect.
  docker compose -f "$lk_dir/docker-compose.livekit.yml" up -d --force-recreate
  ok "SFU (re)created against the freshly rendered config; the gateway already has LIVEKIT_* from step 2"

  # Verify the CAPABILITY, not the container: a running SFU the gateway cannot
  # mint for is the exact failure this step exists to prevent.
  if curl -fsS --max-time 5 http://127.0.0.1:7880 >/dev/null 2>&1; then
    ok "SFU answering on 127.0.0.1:7880"
  else
    warn "SFU not answering on 7880 yet — docker compose -f deploy/livekit/docker-compose.livekit.yml logs"
  fi
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
  • Calls:         $( [ "$DO_MEDIA" = "true" ] \
                        && echo "bundled SFU is up on $TURN_DOMAIN. Open UDP 3478, 7882-7892 and 50000-60000, and put TLS in front of 5349." \
                        || echo "no SFU — /v1/channels/*/video-token returns 503 (a supported state). Re-run with --with-media, or set LIVEKIT_URL/LIVEKIT_API_KEY/LIVEKIT_API_SECRET to use an existing one." )

Re-running this script is safe: it won't rotate your JWT secret or wipe data.
EOF
