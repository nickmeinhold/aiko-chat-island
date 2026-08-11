#!/usr/bin/env bash
# cert-tree-contract.sh — the "third box" (DESIGN §4b, Tesla): prove the renewal
# trigger's validate-before-restart logic behaves the SAME against a cert tree
# shaped like EITHER box topology, and FAIL-CLOSES on a half-written/mismatched
# pair. No docker, no live endpoint — pure logic contract for cert-restart.sh.
#
# Topologies exercised:
#   host-FS leaf dir      (enspyr systemd Caddy):  .../turn.enspyr.co/{crt,key}
#   docker-volume subpath (imagineering container): <vol>/turn.imagineering.cc/{crt,key}
# Both reduce to "a leaf dir holding <domain>.crt + <domain>.key"; the contract is
# that the validation is identical and topology-independent.
set -euo pipefail
FAILS=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Same check cert-restart.sh performs before restarting.
valid_pair() {  # $1 crt  $2 key  -> 0 if matched non-empty pair, else 1
  [ -s "$1" ] && [ -s "$2" ] || return 1
  local cm km
  cm="$(openssl x509 -noout -modulus -in "$1" 2>/dev/null | openssl md5)"
  km="$(openssl rsa -noout -modulus -in "$2" 2>/dev/null | openssl md5)"
  [ -n "$cm" ] && [ "$cm" = "$km" ]
}

mk_pair() {  # $1 dir  $2 domain  -> writes a matched self-signed crt/key
  mkdir -p "$1"
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=$2" \
    -keyout "$1/$2.key" -out "$1/$2.crt" >/dev/null 2>&1
}

check() { if eval "$2"; then echo "  ok: $1"; else echo "  FAIL: $1" >&2; FAILS=$((FAILS+1)); fi; }

for topo in "host:turn.enspyr.co:$TMP/varlib/.../certs" "volume:turn.imagineering.cc:$TMP/caddydata/certs"; do
  IFS=: read -r name domain base <<<"$topo"
  leaf="$base/$domain"; mk_pair "$leaf" "$domain"
  echo "[$name topology] leaf=$leaf"
  # 1. valid matched pair -> accepted (would restart)
  check "valid pair accepted" "valid_pair '$leaf/$domain.crt' '$leaf/$domain.key'"
  # 2. mismatched key (half-write / wrong renewal) -> rejected (fail-closed, no restart)
  mk_pair "$TMP/other" "other.example"; cp "$TMP/other/other.example.key" "$leaf/$domain.key"
  check "mismatched key rejected" "! valid_pair '$leaf/$domain.crt' '$leaf/$domain.key'"
  # 3. empty cert (mid-write) -> rejected
  : > "$leaf/$domain.crt"
  check "empty cert rejected" "! valid_pair '$leaf/$domain.crt' '$leaf/$domain.key'"
done

[ "$FAILS" -eq 0 ] && { echo "cert-tree contract: PASS"; exit 0; } || { echo "cert-tree contract: $FAILS FAILURE(S)" >&2; exit 1; }
