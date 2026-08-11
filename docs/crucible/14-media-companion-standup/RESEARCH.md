# RESEARCH — Media companion standup (Heat)

Sourced findings, 2026-08-11. Five mechanism cells for DESIGN.

## 1. pion/turn cert reload — NO hot-reload (restart required)

LiveKit's embedded TURN (pion/turn) loads `turn.cert_file`/`turn.key_file` **once at process start**; a renewed cert on disk keeps serving the old cert until restart. Confirmed [livekit/livekit#3463](https://github.com/livekit/livekit/issues/3463) — feature requested, **closed as not planned**. So **every renewal ⇒ a livekit restart**. Restart is cheap (~seconds, Go binary) and drops only **in-flight ICE negotiation, not established media** — so the blast radius of a renewal restart on multi-tenant imagineering is bounded to calls mid-connect, not calls in progress.

## 2. Pin — `v1.13.5` (and pinning is NOT a no-op)

Stable = **`livekit/livekit-server:v1.13.5`** (the image behind `latest`) — [Docker Hub](https://hub.docker.com/r/livekit/livekit-server/tags), [releases](https://github.com/livekit/livekit/releases). **Behavior-change gotchas** on the recent line, both TURN-relevant:
- **v1.12.0** — TURN will **not relay to private IPs** without explicit config; TURN credentials gained a TTL (default 300s).
- **v1.13.1** — **removed backward-compat for TURN auth without TTL** ([PR #4539](https://github.com/livekit/livekit/pull/4539)).

⇒ If the boxes' current `:latest` predates v1.12, **pinning can change TURN auth/relay behavior**. Treat the pin as a behavior change: re-run the relay test after pinning, don't bundle "fix cert" and "pin" into one unverified step.

## 3. Caddy → external-process cert delivery

The stock Caddy binary **cannot** do either the Namecheap DNS challenge or an on-renewal exec hook — both need a **custom `xcaddy` build**:
- `caddy-dns/namecheap` ([module](https://github.com/caddy-dns/namecheap)) for DNS-01 — needs `api_key`/`user`/`api_endpoint` + a whitelisted `client_ip` (Namecheap API gotcha).
- `mholt/caddy-events-exec` ([module](https://github.com/mholt/caddy-events-exec)) for `on cert_obtained exec …` — **experimental**, runs commands **in background by default**, and has a **permission-denied gotcha when Caddy runs unprivileged** and the command needs docker/root ([community thread](https://caddy.community/t/events-exec-handler-permission-denied/23695)). enspyr's Caddy runs `User=caddy` (unprivileged) → the restart hook would need docker-group / passwordless-sudo shim.

Caddy historically has **no built-in renew hook** ([caddy#1698](https://github.com/caddyserver/caddy/issues/1698)). Cert storage layout: `…/certificates/acme-v02.api.letsencrypt.org-directory/<domain>/<domain>.{crt,key,json}` ([docs](https://caddyserver.com/docs/automatic-https)). Alternative to copying: bind-mount Caddy's cert dir read-only into the LiveKit container.

## 4. DNS-01 vs HTTP-01

DNS-01 (Namecheap module) avoids needing port 80 on `turn.<domain>` but requires the custom build + API creds + IP allowlist. **HTTP-01** works with **stock Caddy** if `turn.<domain>` has an A record → box and port 80 is reachable (Caddy already listens `:80` on both boxes). Both zones (imagineering.cc, enspyr.co) are Namecheap-managed.

## 5. Firewall — open the RELAY RANGE too, both layers

TURN needs **UDP 3478** + **TCP 5349** (LiveKit suggests 443 if no HTTP3). **Non-obvious trap:** pion/turn allocates relayed-media ports from `turn.relay_range_start`/`relay_range_end`, **default `1024–30000`** — opening only 3478/5349 is insufficient ([livekit#3164](https://github.com/livekit/livekit/issues/3164), [config-sample.yaml](https://github.com/livekit/livekit/blob/master/config-sample.yaml)). **Narrow the range** (e.g. `50000–60000`) so the hole is bounded, open that UDP range on **both OCI security-list AND host iptables** (double-firewall), verify with an external UDP probe. Keep the SFU's own ICE UDP range distinct from the TURN relay range.

## 6. Forced-relay verification — check candidate PROTOCOL, not just type

Force relay-only: `rtcConfig = { iceTransportPolicy: 'relay' }` ⇒ only TURN candidates considered ([LiveKit KB](https://kb.livekit.io/articles/1724892785-establishing-media-connection-firewall-troubleshooting)). Inspect `chrome://webrtc-internals` selected candidate pair. **Critical:** a `relay` candidate can still be the media node's **embedded TURN over UDP** — to prove the TLS/TCP 5349 path you must confirm the selected relay candidate's **protocol is TCP/TLS**, not merely that its type is `relay` ([livekit#3971](https://github.com/livekit/livekit/issues/3971)). The protocol field is the real acceptance gate.

## Cross-cutting note

The research's default (custom xcaddy with two non-standard modules) is the heaviest path — see DESIGN §Fold for the simpler mechanism that survived the self-strike.
