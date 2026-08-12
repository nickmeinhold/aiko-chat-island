# TURN-over-TLS on :443 — build + cutover runbook (enspyr, task #4)

Architecture: [`haproxy.cfg`](haproxy.cfg). Shape C / `external_tls`. See
`docs/crucible/turn-tls-443-relay/DESIGN.md` for the tempered design + why this beats the
caddy-l4 mux. **The cutover + rollback are SCRIPTS, not prose** — this runbook orchestrates
them; it is not the thing you hand-execute on the live front door.

Artifacts in this dir:

| File | Role |
|---|---|
| `haproxy.cfg` | the :443 SNI mux (installed to `/etc/haproxy/haproxy.cfg`) |
| `Caddyfile.mux` | Caddy moved to loopback:8443 + turn cert-issuance block (HTTP-01 forced) |
| `cutover.sh` | fail-closed, 4-artifact, auto-rollback-on-red live cutover |
| `rollback.sh` | restores all 4 artifacts; safe to run standalone anytime |
| `haproxy-cert-sync.sh` + `.service` + `.timer` | the cert-renewal fix (see below) |

## The 4 coupled artifacts (the cage-match P0)

A Shape-C cutover changes **four** pieces of state, and rollback must restore **all four** —
the earlier "3-artifact rollback" left LiveKit on `external_tls` (**plaintext TURN on public
:5349**) after a "successful" rollback. The four:

| # | Artifact | Cutover | Rollback |
|---|---|---|---|
| 1 | `iptables` :5349 | DROP non-loopback (**first**, before plaintext) | remove DROP (**last**, after TLS restored) |
| 2 | `livekit.yaml` | `external_tls: true`, drop cert/key, restart | restore `.stock` (TLS on 5349), restart |
| 3 | `/etc/caddy/Caddyfile` | → `Caddyfile.mux` (loopback:8443) | restore `.stock` (public :443) |
| 4 | HAProxy | install + start on :443 | stop (frees :443) |

`rollback.sh` orders these so **plaintext TURN is never publicly reachable** and **:443 is
never owned by two processes**: stop HAProxy → Caddy back on :443 → LiveKit back to TLS →
**hard-gate (openssl handshake on 5349 must present a cert)** → only then reopen the firewall.

## Ports after cutover

| Port | Before | After |
|---|---|---|
| `:443` (public) | Caddy (all TLS) | **HAProxy** — SNI mux |
| `127.0.0.1:8443` | — | Caddy (all sites, moved off :443) |
| `127.0.0.1:8444` | — | HAProxy turn-TLS terminator (holds turn cert) |
| `:5349` | LiveKit TURN/TLS (own cert) | LiveKit TURN **plaintext** (external_tls), **loopback-firewalled** |
| `:80` | Caddy (HTTP-01) | Caddy (HTTP-01) — **unchanged**, the renewal path |
| `:3478/udp`, relay 50000-60000 | LiveKit | unchanged |

## Cert renewal (the P1 time-bomb fix)

Once HAProxy owns :443, a TLS-ALPN-01 renewal for `turn.enspyr.co` would hit HAProxy and
fail silently → cert rots → :443 TURNS dies ~89d out. Closed two ways:

1. **Deterministic HTTP-01.** `Caddyfile.mux` keeps a loopback-only `turn.enspyr.co` block
   with `tls { issuer acme { disable_tlsalpn_challenge } }` — Caddy stays the ACME client and
   renews via HTTP-01 on :80 (untouched). Keeping the block also prevents "issuance dies when
   you unstub" (Tesla).
2. **`haproxy-cert-sync.timer`** (hourly, fingerprint-gated) rebuilds HAProxy's PEM from
   Caddy's renewed cert + `systemctl reload haproxy` (graceful). This is the Shape-C form of
   task #3's cert timer: sync caddy→haproxy, **not** restart LiveKit (external_tls means
   LiveKit no longer holds the cert). The PEM is written `0640 root:haproxy` via atomic
   rename — the turn key crosses into haproxy's uid space locked, never world-readable (Tesla F8).

## Build + PROVE OFF THE LIVE :443 (do ALL of this before running cutover.sh)

`cutover.sh` refuses to run unless `OFF443_PROVEN=1` — you assert you have:

1. `haproxy -c -f haproxy.cfg` → exit 0 (config check, no bind). ✅ validated off-prod.
2. Staged a full parallel chain on ALT ports (HAProxy fe443→`:9443`, terminator `:8444`, a
   **scratch** LiveKit / scratch config — NEVER flip the live process as a rehearsal oscillator,
   Tesla) and proven a real TURNS allocation THROUGH `:9443` with `b3_relay_probe` pointed at
   the alt port.
3. Negative tests on the alt chain: chat + livekit SNI passthrough still 200/WSS; unknown SNI;
   `acme-tls/1` ALPN; malformed/slow ClientHello; non-TLS junk (rejected); concurrent WSS during
   a HAProxy reload.
4. Cert-sync rehearsal: force a `turn.enspyr.co` renew, run `haproxy-cert-sync.sh`, confirm the
   served-cert fingerprint on the alt chain == Caddy's store.

## Cutover (the ONE irreversible step)

```bash
sudo OFF443_PROVEN=1 bash deploy/media/turn-443/cutover.sh
```

It runs the sequenced state machine (Phase 0 preconditions → stage backups → 2.1 firewall
5349 → 2.2 flip LiveKit → 2.3 move Caddy → 2.4 start HAProxy → Phase 3 on-box verify → Phase 4
enable cert-sync), auto-rolling-back on any verify failure. It measures + logs the :443 dark
window (Caddy-release → HAProxy-bind).

## Acceptance gate (run from OFF-BOX after cutover.sh reports green)

`deploy/media/b3_relay_probe.py` against enspyr must flip `turns:443` `UNREACHABLE → ALLOCATED`,
UDP relay still allocates, all RFC1918/link-local/loopback/CGNAT sentinels still 403, chat +
signaling green. **If it fails: `sudo bash deploy/media/turn-443/rollback.sh`.**

Also prove INV-1 from **off-box** (the on-box guard is v4+v6 host-INPUT, valid only because
LiveKit is host-networked — cutover asserts that): from your laptop,
`openssl s_client -connect <enspyr-public-ip>:5349` must **fail/refuse** (public plaintext :5349
is closed). A localhost probe can't prove external closure; this can. Repeat over IPv6 if the box
ever gains a public v6 address.

## Gate

**Code cage-match this config + the cutover/rollback scripts before the live cutover** (the
design temper does not cover the implementation). Round 1 (2026-08-12) returned unanimous
REQUEST_CHANGES on the prose-only version; this revision ships the scripts + fixes — re-run
the cage-match on it before deploying.
