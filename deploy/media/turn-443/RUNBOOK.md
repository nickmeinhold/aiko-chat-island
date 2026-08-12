# TURN-over-TLS on :443 — build + cutover runbook (enspyr, task #4)

Architecture: [`haproxy.cfg`](haproxy.cfg). Shape C / `external_tls`. See
`docs/crucible/turn-tls-443-relay/DESIGN.md` for the tempered design + why this beats the
caddy-l4 mux (mature component; `external_tls` dissolves the cert-reload media bounce).

## Ports after cutover

| Port | Before | After |
|---|---|---|
| `:443` (public) | Caddy (all TLS) | **HAProxy** — SNI mux |
| `127.0.0.1:8443` | — | Caddy (all its sites, moved off :443) |
| `127.0.0.1:8444` | — | HAProxy turn-TLS terminator (holds turn cert) |
| `:5349` | LiveKit TURN/TLS | LiveKit TURN **plaintext** (external_tls), localhost-only |
| `:80` | Caddy (HTTP-01) | Caddy (HTTP-01) — **unchanged**, the renewal path |
| `:3478/udp` | LiveKit TURN/UDP | unchanged |

## Config changes (three files)

1. **LiveKit** `~/apps/livekit/livekit.yaml` turn block: add `external_tls: true`; keep
   `tls_port: 5349`; drop `cert_file`/`key_file` (HAProxy owns the cert now); bind 5349 to
   localhost if supported (`bind_addresses: [127.0.0.1]` — but that also moves udp/3478 to
   localhost, which is wrong; instead firewall :5349 to localhost, leave bind wildcard).
2. **Caddy** `/etc/caddy/Caddyfile`: move HTTPS to `127.0.0.1:8443` (global `https_port 8443`
   or per-site `bind 127.0.0.1:8443`); **force HTTP-01** for ALL sites (TLS-ALPN now hits
   HAProxy) — global `acme_dns`? no; use `tls { }` with issuer forcing HTTP-01, or rely on
   :80. KEEP a `turn.enspyr.co` cert-issuance path (site block or tls directive) so Caddy
   still renews it — Tesla's "issuance dies when you unstub" finding.
3. **HAProxy** `/etc/haproxy/haproxy.cfg` = the committed file; cert at
   `/etc/haproxy/certs/turn.enspyr.co.pem` (Caddy fullchain+key concat, rebuilt on renew).

## Build + PROVE OFF THE LIVE :443 (do all of this before touching :443)

1. `apt install haproxy`; `haproxy -c -f haproxy.cfg` (config check, no bind).
2. Stage a full parallel chain on ALT ports (HAProxy fe443→`:9443`, terminator `:8444`,
   a scratch LiveKit or the live one with external_tls on a scratch config) and prove a
   real TURNS allocation THROUGH `:9443` with a modified `b3_relay_probe`/`probe5349.py`
   pointed at the alt port. Green = the mux mechanism works end-to-end, live :443 untouched.
3. Negative tests on the alt chain: chat SNI + livekit SNI passthrough still 200/WSS;
   unknown SNI; `acme-tls/1` ALPN; malformed/slow ClientHello; concurrent WSS during a
   HAProxy reload. (Carnot/Tesla acceptance-gate findings.)
4. Cert-renewal rehearsal: force a turn.enspyr.co renew, confirm the .pem rebuild + HAProxy
   reload serves the NEW cert on the alt chain (served-cert fingerprint == store).

## Guarded cutover (the ONE irreversible step — foreground, verify each)

Single fail-closed script (F1 = THREE-artifact rollback): stage `.stock` copies of the
Caddyfile + HAProxy-absent state + firewall; then move Caddy→8443, LiveKit→external_tls
(restart — brief media blip, windowed), install HAProxy on :443. Immediately verify:
(a) `chat.enspyr.co` 200, (b) `livekit.enspyr.co` WSS upgrade, (c) `b3_relay_probe` off-box
→ `turns:443` ALLOCATED, (d) media round-trip. ANY red → restore all three artifacts +
`systemctl restart caddy` (Caddy back on :443), stop HAProxy. Then localhost-firewall :5349.

## Gate

**Code cage-match the config + cutover script before the live cutover** (this design temper
does not cover the implementation). Acceptance = the off-443 proof (steps 2-4) green, THEN
the cutover verify (a-d) green off-box.
