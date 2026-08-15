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
# CURL EXIT CODES, not "did it fail" (Carnot + Tesla, independently — which is what makes it
# high-confidence). The first version returned "passthrough" on ANY non-success: curl absent
# (127), connection refused (7), timeout (28), TLS verify failed (60). So the instrument built to
# fix a fail-open was itself fail-open. Worse, it interacted with a deliberate choice above:
# turn_tls_ok treats an unverifiable chain as advisory so a private-CA box can deploy, while curl
# verifies HARD — on such a box the GET dies with 60 and reads as green, forever.
#
# So: interpret ONLY the outcomes that actually discriminate, and demand a known-positive control
# first. If curl cannot reach :443 for a name that MUST work, then a failure on the turn name
# says nothing about routing — it says the instrument is blind, and blind must not read as green.
#
# The exit codes below are MEASURED, not reasoned. The first version guessed 52/56 ("empty
# reply"/"reset") for the LiveKit case; the rig returned **28 (timeout)** — pion/turn completes
# the TLS handshake and then simply never speaks HTTP, so curl waits out the clock. That guess
# would have auto-rolled-back every correct cutover. It did, once, on the rig.
#
#   exit 0                  -> an HTTP response came back -> Caddy answered -> MISROUTED
#   exit 28/52/56/35/18/55  -> a peer that accepts TLS and does not speak HTTP -> passthrough,
#                              but ONLY meaningful once the control has proved the box is
#                              reachable at all: a black hole times out identically.
#   exit 7 (refused) / 60 (TLS verify) / 127 (no curl) -> INDETERMINATE, never green.
#
# THE CONTROL IS WHAT MAKES 28 READABLE. Without it, "timed out" is equally "LiveKit is behind
# the mux, correctly silent" and "nothing is there". With a control that succeeded through the
# same :443, only the first reading survives. A caller that passes no control gets a hard
# INDETERMINATE rather than a courtesy pass.
turn_path_is_passthrough() {  # <domain> [control-domain] [port]
  local domain="$1" control="${2:-}" port="${3:-443}" rc
  command -v curl >/dev/null 2>&1 || { echo "[turn-assert] curl absent — cannot discriminate the turn path" >&2; return 2; }

  if [ -z "$control" ]; then
    echo "[turn-assert] no control domain given — a timeout cannot be told from a black hole. Refusing to report passthrough." >&2
    return 2
  fi
  curl -sS -o /dev/null --max-time 8 --resolve "${control}:${port}:127.0.0.1" "https://${control}/" 2>/dev/null || {
    echo "[turn-assert] CONTROL FAILED: an HTTPS GET for ${control} through :${port} did not succeed, so this probe cannot tell a misroute from a broken instrument. Refusing to report passthrough." >&2
    return 2
  }

  curl -sS -o /dev/null --max-time 8 --resolve "${domain}:${port}:127.0.0.1" "https://${domain}/" 2>/dev/null
  rc=$?
  case "$rc" in
    0)                       return 1 ;;   # Caddy answered — misrouted
    28|52|56|35|18|55|97)    return 0 ;;   # accepts TLS, never speaks HTTP — LiveKit's TURN socket
    *) echo "[turn-assert] turn-path probe INDETERMINATE (curl exit $rc for ${domain}): neither a misroute proof nor a passthrough proof." >&2
       return 2 ;;
  esac
}
