#!/usr/bin/env bash
# checks.sh — the assertions the rehearsal is FOR. Sourced by run-matrix.sh.
#
# Design rule: every check must be able to FAIL. A probe that returns "closed" because the
# probe itself is broken proves nothing (feedback_negative_probe_not_negative_fact), so the
# external-vantage probes are validated POSITIVE at CP0 (where the port is genuinely open)
# before their negative result is trusted anywhere else.
TURN_DOMAIN="${TURN_DOMAIN:-turn.enspyr.co}"
CHAT_DOMAIN="${CHAT_DOMAIN:-chat.enspyr.co}"

# The "external" vantage point: a container on docker0. Traffic from it arrives on a
# NON-loopback interface, so the `! -i lo ... -j DROP` rules apply exactly as they would to a
# real internet client. This is what makes an off-box closure claim testable inside one VM.
DOCKER_GW="$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"

ext_tcp_open() {  # ext_tcp_open <port> -> 0 if a container can complete a TCP connect
  local port="$1"
  docker run --rm --network bridge busybox:latest \
    timeout 4 nc -z "$DOCKER_GW" "$port" >/dev/null 2>&1
}

port_owner() {    # port_owner <port> -> process name holding it, or "" if unbound
  ss -tlnpH "sport = :$1" 2>/dev/null | grep -oE 'users:\(\("[^"]+' | head -1 | sed 's/.*"//'
}

tls_serves_cert() { # tls_serves_cert <host> <port> <domain>
  # -checkhost, not `grep BEGIN CERTIFICATE` (Tesla): the banner claims "a valid $TURN_DOMAIN
  # cert" while the test only proved SOME cert appeared. Verify the name the caller asked about.
  echo | timeout 8 openssl s_client -connect "$1:$2" -servername "$3" 2>/dev/null \
    | openssl x509 -noout -checkhost "$3" >/dev/null 2>&1
}

cert_fingerprint() { # cert_fingerprint <host> <port> <sni>
  timeout 8 openssl s_client -connect "${1}:${2}" -servername "${3}" </dev/null 2>/dev/null \
    | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2
}

chat_reachable() { curl -sS --max-time 8 -o /dev/null "https://${CHAT_DOMAIN}/" 2>/dev/null; }

# INV-5: what client IP does the app behind Caddy actually see? Returns the echoed XFF.
chat_seen_client_ip() {
  curl -sS --max-time 8 "https://${CHAT_DOMAIN}/" 2>/dev/null | jq -r '.x_forwarded_for // "none"'
}

fw_rule_present() { # fw_rule_present <family> <port>
  "$1" -C INPUT ! -i lo -p tcp --dport "$2" -j DROP 2>/dev/null
}

livekit_running() { [ "$(docker inspect livekit --format '{{.State.Running}}' 2>/dev/null)" = "true" ]; }
livekit_external_tls() { grep -q '^  external_tls: true' /home/ubuntu/apps/livekit/livekit.yaml 2>/dev/null; }

# ---------------------------------------------------------------- composite state report
state_report() {
  local o443; o443="$(port_owner 443)"
  echo "  :443 owner        = ${o443:-<UNBOUND>}"
  echo "  :8443 owner       = $(port_owner 8443)"
  echo "  :5349 owner       = $(port_owner 5349)"
  echo "  livekit running   = $(livekit_running && echo yes || echo NO)"
  echo "  livekit ext_tls   = $(livekit_external_tls && echo yes || echo no)"
  echo "  fw v4 5349 / 8443 = $(fw_rule_present iptables 5349 && echo DROP || echo open) / $(fw_rule_present iptables 8443 && echo DROP || echo open)"
  echo "  fw v6 5349 / 8443 = $(fw_rule_present ip6tables 5349 && echo DROP || echo open) / $(fw_rule_present ip6tables 8443 && echo DROP || echo open)"
  echo "  haproxy unit      = $(systemctl is-enabled haproxy 2>/dev/null)/$(systemctl is-active haproxy 2>/dev/null)"
  echo "  ext :5349 reach   = $(ext_tcp_open 5349 && echo OPEN || echo closed)"
  echo "  ext :8443 reach   = $(ext_tcp_open 8443 && echo OPEN || echo closed)"
  echo "  chat reachable    = $(chat_reachable && echo yes || echo NO)"
}

# ---------------------------------------------------------------- the safety invariants
# These must hold at EVERY checkpoint, before and after a reboot. This is the heart of the
# rehearsal: not "did the happy path work" but "is every intermediate state safe to be in".
PASS=0; FAIL=0
chk() { # chk <description> <0-if-ok>
  if [ "$2" -eq 0 ]; then echo "    PASS  $1"; PASS=$((PASS+1));
  else echo "    FAIL  $1"; FAIL=$((FAIL+1)); fi
}

assert_safety() {
  local label="$1"
  echo "  --- safety invariants [$label] ---"

  # NO-PLAINTEXT (was INV-1). Under passthrough this should be structurally impossible: nothing
  # sets external_tls, so :5349 is always LiveKit's own TLS. The check is KEPT — and kept in this
  # asserting form rather than deleted — precisely because "the config can't do that any more" is
  # the kind of claim that should be measured, not trusted. If external_tls ever reappears (a
  # hand-edit, a stale artifact, a half-run of an old cutover) this fails loudly instead of the
  # box quietly serving plaintext TURN to the internet.
  local ext5349 ext8443
  ext_tcp_open 5349 && ext5349=open || ext5349=closed
  if livekit_external_tls; then
    chk "NO-PLAINTEXT external_tls is set — UNEXPECTED under the passthrough shape" 1
    chk "  ...at least plaintext :5349 is NOT externally reachable" "$([ "$ext5349" = closed ] && echo 0 || echo 1)"
  elif [ "$ext5349" = open ]; then
    tls_serves_cert 127.0.0.1 5349 "$TURN_DOMAIN"
    chk "NO-PLAINTEXT :5349 reachable, and it is LiveKit's own TLS (expected)" $?
  else
    chk "NO-PLAINTEXT :5349 closed externally (also fine)" 0
  fi

  # The backend the mux depends on. A passthrough mux in front of a LiveKit that is not serving
  # a valid cert for TURN_DOMAIN is a turn endpoint that accepts and then dies mid-handshake.
  # FAIL-OPEN BY OMISSION (Tesla): this used to be skipped entirely when :5349 had no owner, so
  # a muxed box with a DEAD turn backend printed pass=N fail=0. If HAProxy owns :443, the whole
  # point of the mux is that :5349 is behind it — an unbound backend is a red, not a silence.
  if [ -n "$(port_owner 5349)" ]; then
    tls_serves_cert 127.0.0.1 5349 "$TURN_DOMAIN"
    chk "BACKEND LiveKit serves a valid $TURN_DOMAIN cert on :5349" $?
  elif [ "$(port_owner 443)" = "haproxy" ]; then
    chk "BACKEND :5349 has NO owner while haproxy fronts :443 — the turn backend is DEAD" 1
  else
    chk "BACKEND :5349 unbound (and :443 is not muxed) — not yet a fault" 0
  fi

  # ADVERTISED-IDENTITY-IS-HELD. The SFU hands clients its node_ip as the address to send media
  # to; if the box does not actually hold that address, every call fails while every listener,
  # cert and route check stays green. Found on this rig 2026-08-14: an `ip addr add` alias did
  # not survive a reboot, LiveKit kept advertising it, and the safety matrix reported pass=5
  # fail=0 over a media plane that could not work. Ports and certs are not the media path.
  local advertised
  advertised="$(grep -E '^[[:space:]]*node_ip:' /home/ubuntu/apps/livekit/livekit.yaml 2>/dev/null | awk '{print $2}')"
  if [ -n "$advertised" ]; then
    ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -qx "$advertised"
    chk "ADVERTISED node_ip ($advertised) is an address this box actually HOLDS" $?
  fi

  # PASSTHROUGH-PATH. A cert check cannot tell passthrough from a misroute, because
  # Caddyfile.mux keeps a turn. site block served from THE SAME store LiveKit mounts — so if the
  # SNI rule dumps turn into default_backend be_caddy, the cert and even its fingerprint match
  # (Tesla). Discriminate the PATH: Caddy answers `respond "turn" 200` to an HTTPS GET; LiveKit's
  # TURN socket cannot speak HTTP. A successful GET means Caddy has the turn SNI — the misroute.
  if [ "$(port_owner 443)" = "haproxy" ]; then
    if curl -sS -o /dev/null --max-time 8 --resolve "${TURN_DOMAIN}:443:127.0.0.1" "https://${TURN_DOMAIN}/" 2>/dev/null; then
      chk "PASSTHROUGH-PATH turn SNI is answered by CADDY, not LiveKit (misrouted)" 1
    else
      chk "PASSTHROUGH-PATH turn SNI is NOT answered by Caddy (real passthrough)" 0
    fi
  fi

  # INV-8: Caddy's HTTPS must not be publicly reachable once it has moved to :8443.
  if [ -n "$(port_owner 8443)" ]; then
    ext_tcp_open 8443 && ext8443=open || ext8443=closed
    chk "INV-8 :8443 NOT externally reachable" "$([ "$ext8443" = closed ] && echo 0 || echo 1)"
  fi

  # INV-2: :443 must have exactly one owner — never two, never (lastingly) zero.
  local owner; owner="$(port_owner 443)"
  chk "INV-2 :443 has exactly one owner (got '${owner:-NONE}')" "$([ -n "$owner" ] && echo 0 || echo 1)"

  # Service continuity: chat must answer in every steady state.
  chat_reachable; chk "chat.enspyr.co reachable" $?
}
