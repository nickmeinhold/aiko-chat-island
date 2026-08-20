# cert-pair.sh — shared cert/key pairing check. Sourced by cert-restart.sh AND
# test/cert-tree-contract.sh so the trigger and its test CANNOT drift (round-1
# Carnot+Tesla: an RSA-only check + RSA-only test was a verifier blind to the
# ECDSA production path).
#
# EC + RSA agnostic: compares the PUBLIC KEY derived from the cert vs the key,
# in DER, via digest. `openssl x509 -modulus` is RSA-only and returns nothing for
# Caddy/LE's default ECDSA leaf — this path works for both families.

cert_pair_matches() {  # $1 crt  $2 key  [$3 domain] -> 0 if matched, non-empty,
                       # unexpired, and (if domain given) valid FOR that domain
  local crt="$1" key="$2" domain="${3:-}" crt_pub key_pub
  [ -s "$crt" ] && [ -s "$key" ] || return 1
  crt_pub="$(openssl x509 -in "$crt" -noout -pubkey 2>/dev/null \
             | openssl pkey -pubin -outform DER 2>/dev/null | openssl dgst -sha256 2>/dev/null)"
  key_pub="$(openssl pkey -in "$key" -pubout -outform DER 2>/dev/null | openssl dgst -sha256 2>/dev/null)"
  [ -n "$crt_pub" ] && [ "$crt_pub" = "$key_pub" ] || return 1
  # Refuse an already-expired cert (a stale pair is not a valid restart target).
  openssl x509 -in "$crt" -noout -checkend 0 >/dev/null 2>&1 || return 1
  # Refuse a renewed-but-WRONG leaf: it must carry TURN_DOMAIN as a DNS SAN (round-2
  # Carnot). Portable exact-SAN match (NOT `-checkhost`, absent on LibreSSL; NOT a
  # `-w` grep, which prefix-matches turn.<domain>.evil.com). Caddy issues exact-name
  # leaves (no wildcard), so exact per-entry compare is correct here.
  [ -z "$domain" ] || cert_has_dns_san "$crt" "$domain"
}

cert_has_dns_san() {  # $1 crt  $2 domain -> 0 if $2 is an exact DNS SAN of the cert
  local sans d
  # Prefer the machine-readable extension dump (OpenSSL 3, Kelvin r3); fall back to
  # the -text parse on LibreSSL, which lacks -ext. Either way, split on DNS: and
  # exact-compare each entry (no -w prefix footgun, no wildcard handling — Caddy
  # issues exact-name leaves).
  sans="$(openssl x509 -in "$1" -noout -ext subjectAltName 2>/dev/null)"
  [ -n "$sans" ] || sans="$(openssl x509 -in "$1" -noout -text 2>/dev/null | grep -A1 'Subject Alternative Name')"
  for d in $(printf '%s' "$sans" | tr ',' '\n' | sed -n 's/.*DNS:\([^,[:space:]]*\).*/\1/p'); do
    [ "$d" = "$2" ] && return 0
  done
  return 1
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
