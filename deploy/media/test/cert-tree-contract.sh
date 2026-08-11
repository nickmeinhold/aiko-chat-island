#!/usr/bin/env bash
# cert-tree-contract.sh — the "third box" (DESIGN §4b). Proves the renewal
# trigger's validate-before-restart logic behaves the same across BOTH box
# topologies AND BOTH key families, and fail-closes on a bad pair. No docker, no
# live endpoint.
#
# Sources the SAME cert_pair_matches() the trigger uses (round-1 Carnot+Tesla: a
# separate RSA-only test was a verifier blind to the ECDSA production path — the
# check must fail differently from the checked, and it can't if it's a copy).
set -euo pipefail
cd "$(dirname "$0")/.."; . lib/cert-pair.sh
FAILS=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

mk_rsa() { mkdir -p "$1"; openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=$2" -keyout "$1/$2.key" -out "$1/$2.crt" >/dev/null 2>&1; }
mk_ec()  { mkdir -p "$1"; openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 30 -subj "/CN=$2" -keyout "$1/$2.key" -out "$1/$2.crt" >/dev/null 2>&1; }
check()  { if eval "$2"; then echo "  ok: $1"; else echo "  FAIL: $1" >&2; FAILS=$((FAILS+1)); fi; }

# host-FS (enspyr systemd Caddy) and docker-volume (imagineering) topologies ×
# RSA and EC (Caddy/LE DEFAULT) key families.
for topo in "host:turn.enspyr.co:$TMP/varlib/certs" "volume:turn.imagineering.cc:$TMP/caddydata/certs"; do
  IFS=: read -r name domain base <<<"$topo"
  for fam in rsa ec; do
    leaf="$base/$fam/$domain"; "mk_$fam" "$leaf" "$domain"
    echo "[$name topology / $fam] leaf=$leaf"
    check "$fam valid pair accepted" "cert_pair_matches '$leaf/$domain.crt' '$leaf/$domain.key'"
    # mismatched key from a different pair -> rejected (fail-closed)
    mk_ec "$TMP/other/$fam" other.example; cp "$TMP/other/$fam/other.example.key" "$leaf/$domain.key"
    check "$fam mismatched key rejected" "! cert_pair_matches '$leaf/$domain.crt' '$leaf/$domain.key'"
    # empty cert (mid-write) -> rejected
    : > "$leaf/$domain.crt"
    check "$fam empty cert rejected" "! cert_pair_matches '$leaf/$domain.crt' '$leaf/$domain.key'"
  done
done

[ "$FAILS" -eq 0 ] && { echo "cert-tree contract: PASS"; exit 0; } || { echo "cert-tree contract: $FAILS FAILURE(S)" >&2; exit 1; }
