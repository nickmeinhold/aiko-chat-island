#!/usr/bin/env bash
# turn-assert.sh — the shared assertions for the TURN-on-:443 mux. Sourced by cutover.sh,
# migrate-to-passthrough.sh, and the rehearsal harness.
#
# WHY THIS FILE EXISTS. Cage-match round 2 found that a cert check cannot prove passthrough, and
# the fix was applied to cutover.sh, checks.sh and faults.sh — three of the four places that make
# the claim. Round 3 (Tesla) found the fourth: migrate-to-passthrough.sh, which is the script that
# runs on the ALREADY-MUXED LIVE box, with no dark window and no second chance. It was still
# printing "MIGRATION COMPLETE" over what could be Caddy wearing LiveKit's face.
#
# That is the tell that the fix was aimed at instances rather than at the class. So the assertions
# live in ONE place now: a future correction lands everywhere or nowhere, and "we fixed 3 of 4"
# stops being a reachable state.

# ---------------------------------------------------------------- input validation
# TURN_DOMAIN reaches `sed s///` (where / and & are syntax) and grep patterns, as root, writing
# the front door's config. An allowlist is the whole fix — anything outside it is not a hostname.
turn_domain_valid() {  # turn_domain_valid <domain>
  case "$1" in
    ""|*[!a-zA-Z0-9.-]*|.*|-*|*.|*-) return 1 ;;
    *) return 0 ;;
  esac
}

# ---------------------------------------------------------------- TLS identity
# `-checkhost` alone verifies the NAME and nothing else — not expiry, not the chain (Carnot).
# Messages that then say "valid cert" are overclaiming, and an expired LiveKit cert would sail
# through Phase 0 and let a live :443 move. So: name AND not-expired AND (where a trust store can
# see it) a verifying chain.
#
# CHAIN VERIFICATION IS ADVISORY, DELIBERATELY. On a box whose turn cert comes from a private CA
# the operator has not installed system-wide, a hard chain requirement would fail-CLOSED on a
# perfectly good cert — and this assertion gates a deploy. Name + expiry are hard; chain failure
# warns. Stated so the asymmetry reads as a decision, not an oversight.
turn_tls_ok() {  # turn_tls_ok <host> <port> <domain> [expiry-margin-seconds]
  local host="$1" port="$2" domain="$3" margin="${4:-86400}" pem
  pem="$(echo | timeout 10 openssl s_client -connect "${host}:${port}" -servername "$domain" 2>/dev/null)" || return 1
  printf '%s' "$pem" | openssl x509 -noout -checkhost "$domain" >/dev/null 2>&1 || return 1
  printf '%s' "$pem" | openssl x509 -noout -checkend "$margin" >/dev/null 2>&1 || return 2
  if ! echo | timeout 10 openssl s_client -connect "${host}:${port}" -servername "$domain" \
        -verify_return_error -verify_hostname "$domain" >/dev/null 2>&1; then
    echo "[turn-assert] NOTE: ${host}:${port} presents a valid, unexpired cert for ${domain}, but its chain does not verify against this box's trust store (private CA, or a missing intermediate). Not treated as fatal — see turn-assert.sh." >&2
  fi
  return 0
}

# Human-readable reason for the last turn_tls_ok return code, so callers stop saying "valid cert"
# when they mean one specific thing about it.
turn_tls_reason() {  # turn_tls_reason <rc> <host> <port> <domain>
  case "$1" in
    1) echo "no TLS handshake, or the cert is not valid for $4, on $2:$3" ;;
    2) echo "the cert on $2:$3 is valid for $4 but EXPIRES within the margin — renew before deploying, not during" ;;
    *) echo "ok" ;;
  esac
}

# ---------------------------------------------------------------- the PATH, not the cert
# The one that cost two rounds to learn. Caddyfile.mux deliberately keeps a `turn.` site block,
# and Caddy serves it from THE SAME cert store LiveKit mounts. So if the SNI rule misroutes turn
# into default_backend be_caddy, the cert is BYTE-IDENTICAL: -checkhost passes and even a
# fingerprint comparison against :5349 passes. RED-proved on the rig 2026-08-15.
#
# The discriminator has to be something only ONE of the two can do. Caddy answers an HTTPS GET
# (`respond "turn" 200`); LiveKit's TURN socket cannot speak HTTP at all. So:
#   GET succeeds  -> Caddy has the turn SNI  -> MISROUTED
#   GET fails     -> LiveKit has it          -> real passthrough
# Pinned to 127.0.0.1 so it measures THIS box, never whatever DNS resolves to.
turn_path_is_passthrough() {  # turn_path_is_passthrough <domain> [port]
  local domain="$1" port="${2:-443}"
  if curl -sS -o /dev/null --max-time 8 --resolve "${domain}:${port}:127.0.0.1" \
       "https://${domain}/" 2>/dev/null; then
    return 1   # an HTTP response came back => Caddy answered => misrouted
  fi
  return 0
}
