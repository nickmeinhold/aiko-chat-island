# cert-pair.sh — shared cert/key pairing check. Sourced by cert-restart.sh AND
# test/cert-tree-contract.sh so the trigger and its test CANNOT drift (round-1
# Carnot+Tesla: an RSA-only check + RSA-only test was a verifier blind to the
# ECDSA production path).
#
# EC + RSA agnostic: compares the PUBLIC KEY derived from the cert vs the key,
# in DER, via digest. `openssl x509 -modulus` is RSA-only and returns nothing for
# Caddy/LE's default ECDSA leaf — this path works for both families.

cert_pair_matches() {  # $1 crt  $2 key  -> 0 if a matched, non-empty, unexpired pair
  local crt="$1" key="$2" crt_pub key_pub
  [ -s "$crt" ] && [ -s "$key" ] || return 1
  crt_pub="$(openssl x509 -in "$crt" -noout -pubkey 2>/dev/null \
             | openssl pkey -pubin -outform DER 2>/dev/null | openssl dgst -sha256 2>/dev/null)"
  key_pub="$(openssl pkey -in "$key" -pubout -outform DER 2>/dev/null | openssl dgst -sha256 2>/dev/null)"
  [ -n "$crt_pub" ] && [ "$crt_pub" = "$key_pub" ] || return 1
  # Also refuse an already-expired cert (a stale pair is not a valid restart target).
  openssl x509 -in "$crt" -noout -checkend 0 >/dev/null 2>&1
}

_enddate_to_epoch() {  # $1 = openssl notAfter string -> epoch
  [ -n "$1" ] || return 1
  date -d "$1" +%s 2>/dev/null || date -jf '%b %e %T %Y %Z' "$1" +%s 2>/dev/null
}

cert_file_not_after_epoch() {  # $1 crt file -> epoch on stdout
  _enddate_to_epoch "$(openssl x509 -in "$1" -noout -enddate 2>/dev/null | cut -d= -f2)"
}

# Probe the LIVE TURNS endpoint and return the SERVED leaf cert's notAfter epoch.
# This is what pion/turn actually presents (its in-memory cert), which can lag the
# freshly-renewed file on disk (round-2 catch: disk-live != process-live).
served_cert_not_after_epoch() {  # $1 host  $2 port  $3 sni -> epoch on stdout; rc 1 if no handshake
  local pem
  pem="$(echo | timeout 15 openssl s_client -connect "$1:$2" -servername "$3" 2>/dev/null \
         | openssl x509 2>/dev/null)"
  [ -n "$pem" ] || return 1
  _enddate_to_epoch "$(echo "$pem" | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"
}
