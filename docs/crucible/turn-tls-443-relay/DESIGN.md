# DESIGN — TURN-over-TLS on :443 (enspyr media plane)

Status: **TEMPERED → RE-CAST v2 (UN-RE-TEMPERED)** · Scope: **enspyr only** · Task #4
Companion: [`CRUCIBLE.md`](CRUCIBLE.md) (the case + falsifier), [`RESEARCH.md`](RESEARCH.md) (Heat findings)

---

## 🔧 RE-CAST v3 (2026-08-12, post-falsifiers) — LiveKit-blessed `external_tls`

Phase-0 falsifiers resolved: **B1 TRUE** (LiveKit has `turn.bind_addresses`) but Shape B
(second public IP) was **vetoed on cost** (billable reserved IP). **C2 FALSE** (no cert
hot-reload). So the shape is **C — a mature L4 SNI proxy on :443** — and reading LiveKit's
own `config-sample.yaml` gave the key refinement: **`external_tls: true`**. LiveKit's
documented recommendation for a shared :443 is *"place a L4 load balancer in front ... set
external_tls to true"* — the proxy terminates TLS, LiveKit takes PLAINTEXT on tls_port.

**This dissolves the temper's worst finding (F3/C2 cert-reload media bounce):** LiveKit no
longer holds the cert, so there is no stale-cert-after-renewal restart. The terminating
proxy (HAProxy) owns the cert and hot-reloads it gracefully (no dropped connections).

**Chosen: HAProxy** (mature, temper-aligned over experimental caddy-l4) on :443, SNI-routing:
`turn.enspyr.co` → terminate TLS → plaintext to LiveKit :5349; else → raw passthrough to
Caddy (moved to 127.0.0.1:8443). **Built + config-validated (`haproxy -c` exit 0) 2026-08-12,
off-prod.** Artifacts + full cutover/rollback runbook: **`deploy/media/turn-443/`**
([`haproxy.cfg`](../../../deploy/media/turn-443/haproxy.cfg),
[`RUNBOOK.md`](../../../deploy/media/turn-443/RUNBOOK.md)). Still-live temper findings folded
into the runbook: cert-issuance-survives-unstubbing (Tesla), fail-closed HTTP-01, behavioral
negative tests, single 3-artifact-rollback cutover script. NEXT: prove the full chain
off-:443 on enspyr, code-cage-match the config, then the guarded cutover.

---

## ⚔️ TEMPER VERDICT (PR #130, 2026-08-12) → RE-CAST v2

**4-way design cage-match (Maxwell + Kelvin + Carnot + Tesla; Wu dark on a Kimi 402).
UNANIMOUS REQUEST_CHANGES.** The ore SURVIVES (TURNS must exist on :443; advertise-5349
is provably dead) but **Shape A as primary is INVALIDATED.** Every fatal finding rhymes on
one principle: *never put experimental pre-1.0 code in front of the shared :443 acceptor
before falsifying the cheaper shapes that don't touch it at all.* The re-cast below flips
the design accordingly. **This v2 is UN-RE-TEMPERED — it needs a fresh strike once Phase 0
picks the shape (a substantial recast is itself un-struck).**

**What flipped:**
1. **Falsify B1 FIRST (was: "don't block A on B1").** B1 (can LiveKit bind TURN/TLS to a
   specific IP?) is a ~1hr probe that, if true, yields **Shape B** with ZERO :443 blast
   radius. Building the experimental mux before running it is backwards. → **Phase 0.**
2. **New Shape C — mature L4-in-front (HAProxy / nginx `stream` / sslh), STOCK Caddy.**
   SNI peel `turn→127.0.0.1:5349`, else→Caddy:8443. Dissolves experimental
   caddy-l4-in-the-chat-process without per-IP LiveKit binds. LiveKit's own docs assume an
   LB on 443. → priced in Phase 0.
3. **Shape A (caddy-l4 `listener_wrappers`) demoted to LAST RESORT** — only if B1 is dead
   AND no mature L4-front is viable. "Caddy's :443 untouched" was fiction: the wrapper IS
   the front door for chat + WSS + video.
4. **C2 (LiveKit SIGHUP cert-reload?) resolved BEFORE shipping restart-as-default** — a
   scheduled full-media bounce every 60-90d is a bug, not a tradeoff (F3).

**Fold misses the panel caught (folded into the re-cast, must appear in whichever shape wins):**
- **Cert issuance dies with the turn stub (Tesla):** deleting `turn.enspyr.co { respond }`
  may stop Caddy *issuing/renewing* that cert (Caddy issues for configured site names).
  Need an explicit cert-management identity for `turn` that survives its SNI no longer
  hitting Caddy's terminator. HTTP-01-on-:80 is necessary, not sufficient.
- **Cert private-key ownership boundary (Carnot F8):** LiveKit reading Caddy's ACME private
  key — permissions/group/storage-path/compromise-blast-radius unstated. BOTH shapes.
- **SNI-only routing ⇒ no HTTP on turn:443 (Carnot F9):** health checks must speak TURNS.
- **ACME disable must be fail-closed (Tesla+Carnot):** "assert HTTP-01" is soft — Caddy
  picks challenge randomly, `disable_tlsalpn_challenge` unreliable (#7612). Use deterministic
  JSON `challenges.tls-alpn.disabled` for the host.
- **A2 over-claimed:** `caddy validate` ≠ layer4 semantics (route order blackholing ACME,
  passthrough that works for openssl but not a real TURN client, ClientHello fragmentation).
  Needs per-traffic-class behavioral tests + staging soak/canary.
- **Acceptance gate additions:** cert-reload with mux LIVE (not the reused pre-mux proof);
  served-cert fingerprint == store after rotation on turns:443; negative tests
  (unknown/chat/livekit SNI, acme-tls/1, malformed/slow ClientHello, concurrent WSS during
  reload); forced-relay MEDIA through 443 (ALLOCATED ≠ media; relay_range Q5 UNVERIFIED).
- **Cutover is a cold cut (Carnot F6+Tesla):** single fail-closed cutover *script* (validate
  both configs → install → restart → probe → else restore ALL THREE artifacts: binary +
  Caddyfile + 5349 bind/firewall), loopback test on an alt port first, soak before closing
  public 5349. F1 rollback is THREE artifacts, not two.

## Re-cast build order v2 (core-first, evidence-gated)

- **Phase 0 — FALSIFIERS FIRST (cheap, read-only, pick the shape):**
  (a) **B1:** grep LiveKit v1.13.5 config/source for a per-IP TURN/TLS bind address; if it
  exists, prove it binds only a secondary IP on a scratch box. (b) **L4-front viability:**
  can HAProxy/nginx `stream` SNI-peel `turn.enspyr.co`→5349 with stock Caddy on an internal
  port, and does it survive WSS + ClientHello edge cases? (c) **C2:** does `livekit-server`
  reload its TURN cert on SIGHUP (no full restart)? Outcomes select the shape:
  **B1 true → Shape B** (second IP, zero :443 touch) · **else L4-front viable → Shape C**
  (stock Caddy, boring proxy) · **else → Shape A** (experimental caddy-l4, last resort).
- **Phase 1 — cert lifecycle (shape-independent, task #3):** cert-management identity for
  `turn` that survives (Tesla finding), private-key ownership boundary (F8), fail-closed
  tls-alpn disable (F2), SIGHUP-or-restart reload proven with the served-cert fingerprint
  test. Independently valuable; precondition for #4 not silently rotting.
- **Phase 2 — build the WINNING shape**, with the behavioral acceptance gate + single
  fail-closed cutover script + soak. Then **cage-match the CODE** (this design temper does
  not substitute for the implementation strike).

**Everything below is the v1 CAST that the temper struck. Kept for the audit trail; the
v2 re-cast above supersedes the "Position" and "Build order" sections.**

---

## Problem

LiveKit v1.13.5's embedded TURN allocates a real relay over TLS on `:5349` today
(valid Caddy-ACME cert, proven live 2026-08-12 both islands). But the SFU advertises
its TLS relay to joining clients as **`turns:turn.enspyr.co:443?transport=tcp`** — port
443, which Caddy owns and answers with a `respond "turn" 200` HTTP stub. A TURNS client
completes TLS, sends its first STUN Allocate, and Caddy's HTTP parser waits for a request
that never arrives → **timeout**. Clients whose only egress is 443 (UDP + 5349 blocked —
the exact population TURN/TLS exists to serve) cannot relay.

**The advertised port is not configurable.** `roommanager.go:1067` hardcodes
`fmt.Sprintf("turns:%s:443?transport=tcp", Domain)`; `turn.tls_port` sets only the listen
socket. So the fix is not "advertise 5349" — it is **serve a working TURNS on :443**.

## Success criterion (acceptance gate)

`deploy/media/b3_relay_probe.py` run off-box against enspyr flips the `turns:443`
endpoint from `UNREACHABLE` → `ALLOCATED` (a real relay address returned), **AND**:
- UDP relay (:3478) still allocates; all RFC1918/link-local/loopback sentinels still 403;
- `chat.enspyr.co` (HTTPS) and `livekit.enspyr.co` (WSS signaling) still fully working;
- the `turn.enspyr.co` cert still auto-renews AND the renewed cert is actually served to
  relayers (see the cert-reload landmine — §Cert reload).

## Ground truth (verified, do not re-test)

| Fact | Value |
|---|---|
| LiveKit TURN TLS on :5349 | allocates a real relay, valid cert (proven) |
| Advertised TURNS URI | `turns:turn.enspyr.co:443?transport=tcp` (hardcoded) |
| Caddy | v2.11.4 stock (no layer4), **systemd binary**, `/etc/caddy/Caddyfile` |
| enspyr Caddy sites | chat→8095, livekit→7880 (WSS), turn→`respond "turn" 200` stub |
| Box network | single VNIC `enp0s6`, priv 10.0.0.4, pub 158.179.17.233 (1:1 NAT) |
| :80 | open, Caddy — **HTTP-01 ACME path unaffected by any :443 change** |
| :5349 | `*:5349` today, firewall-open; would drop to localhost-only under mux |
| Firewall | 443/80/5349/3478 open, relay-range 50000-60000/udp open; **no new ports needed** |
| caddy-l4 | v0.1.2 pins Caddy v2.11.4 exactly (matches box), needs Go ≥1.25.1; **experimental (pre-1.0)** |

## Candidate shapes

### Shape A — layer4 SNI mux on :443 (PRIMARY, research-validated)

Rebuild Caddy with `github.com/mholt/caddy-l4@v0.1.2` (xcaddy), and use the documented
**`listener_wrappers`** idiom so Caddy keeps `:443` natively — layer4 is a thin
ClientHello-peek wrapper that peels off *only* the TURN SNI before Caddy's TLS terminator,
everything else falls through untouched:

```caddyfile
{
  servers :443 {
    listener_wrappers {
      layer4 {
        @turn tls sni turn.enspyr.co
        route @turn {
          proxy {
            upstream 127.0.0.1:5349   # raw TCP passthrough — LiveKit terminates TLS itself
          }
        }
      }
      tls   # non-TURN SNIs fall through to Caddy's normal TLS + HTTP handlers
    }
  }
}
# existing chat/livekit/turn site blocks unchanged EXCEPT the turn stub is deleted
```

- **Passthrough, not termination** (RESEARCH q1/q2): the `tls` matcher parses the raw
  ClientHello and matches SNI without decrypting; a `proxy` with no `tls` handler forwards
  the byte stream end-to-end, so the client's TLS session is with LiveKit:5349. LiveKit's
  cert (Caddy-ACME `turn.enspyr.co`) is what the client validates — unchanged from today.
- **Blast radius:** the wrapper fronts ALL :443 (chat + signaling + video). Mitigations:
  `listener_wrappers` keeps Caddy's own :443/TLS/HTTP intact (minimal restructure, not a
  move to :8443); `caddy validate` before reload; **instant rollback = swap the stock
  binary back + `systemctl restart caddy`** (keep `/usr/bin/caddy.stock`).
- **:5349 → localhost-only** after cutover so 443 is the sole public TLS-TURN ingress
  (defense-in-depth; mind the OCI double-firewall — local iptables AND security list).

### Shape B — second public IP for turn.enspyr.co (LOWER blast radius, GATED)

Assign a secondary private IP on `enp0s6` + a reserved OCI public IP; point
`turn.enspyr.co` DNS at it; bind **LiveKit `tls_port: 443` on that IP only**. Caddy's
:443 on the primary IP is **never touched** — chat + signaling carry zero risk. No
experimental Caddy module, no rebuild.

- **THE GATE (claim-to-falsify B1):** LiveKit binds `*:tls_port` (all interfaces) — we
  observed `*:5349`. Setting `tls_port:443` would make LiveKit bind `*:443` and **collide
  with Caddy**. Shape B is viable ONLY if LiveKit's embedded TURN can bind TURN/TLS to a
  *specific* IP (and Caddy is pinned to the primary IP via `bind 10.0.0.4`). If LiveKit
  has no per-IP TURN bind, Shape B is **dead** — verify before choosing B.
- Extra moving parts vs A: OCI control-plane (reserved public IP, ~small cost), OS
  secondary-IP config persisted across reboot (netplan/cloud-init), Namecheap DNS change.

**Position:** ship **A** (self-contained on the box, no OCI control-plane dependency,
research-confirmed viable). Keep **B** as the preferred shape *iff* B1 clears in a cheap
follow-up — it removes the coupling rather than guarding the shared door. Do NOT block A
on resolving B1. Temper should challenge this ordering.

## Cert reload — the landmine (applies to BOTH shapes, MANDATORY)

LiveKit terminates the passthrough/second-IP TURNS with Caddy's ACME `turn.enspyr.co`
cert but **does not watch Caddy's cert store**. Whichever challenge renews the cert
(~60–90 d), LiveKit keeps serving the STALE cert to relayers until restarted → TURNS
silently breaks weeks after cutover, and the "does the mux work?" test passes right over
it. **`deploy/media/cert-restart.timer` (task #3) is exactly this reload hook** — it must
be deployed + wired AS PART OF THIS CHANGE, not deferred. Acceptance includes: after a
simulated cert rotation, LiveKit serves the NEW cert (served-cert alarm + restart proven).
This couples task #4 and task #3: neither is "done" without the other.

## ACME renewal for turn.enspyr.co (RESEARCH q3)

HTTP-01 on :80 is untouched by any :443 change → renewal keeps working. One hazard: the
TLS-ALPN-01 challenge for `turn.enspyr.co` arrives on :443 with `acme-tls/1` ALPN; under
Shape A the SNI-match would route it to LiveKit and break it. Mitigation: rely on HTTP-01
(disable tls-alpn for that cert, or add an `alpn acme-tls/1` carve-out route to internal
Caddy). Confirm Caddy still picks HTTP-01 given :80 open.

## Build order (core-first, each step independently useful)

1. **Pre-flight, read-only:** confirm on a scratch box / locally that
   `xcaddy build v2.11.4 --with github.com/mholt/caddy-l4@v0.1.2` produces a binary and
   `caddy validate` rejects a deliberately-broken layer4 config. (No prod touch.)
2. **Deploy cert-reload first (task #3):** land `deploy/media` + `.env` + wire
   `cert-restart.timer` on enspyr and prove a simulated rotation restarts LiveKit onto the
   new cert. This is independently valuable (fixes a latent silent-expiry bug) and is a
   precondition for #4 not silently rotting.
3. **Build + stage both rollback artifacts** (F1): custom binary beside stock
   (`/usr/bin/caddy.l4` + preserve `/usr/bin/caddy.stock`), and stage the new Caddyfile as
   `.l4` while preserving `/etc/caddy/Caddyfile.stock` (current, with the turn stub). `caddy
   validate` the new Caddyfile with the l4 binary WITHOUT reloading. Assert Caddy issues the
   turn.enspyr.co cert via **HTTP-01** (F2 — tls-alpn disabled for that host).
4. **Cutover (the irreversible step, foreground, verify-before-next):** swap binary,
   `systemctl restart caddy`; immediately verify (a) chat.enspyr.co 200, (b) livekit WSS
   upgrade, (c) `b3_relay_probe` → `turns:443` ALLOCATED. Any failure → **rollback BOTH
   binary AND Caddyfile** (see Fold F1). Then localhost-bind :5349 + close its public
   firewall.
5. **Acceptance:** full `b3_relay_probe` off-box: turns:443 ALLOCATED, UDP intact, RFC1918
   refused; chat + signaling green; cert-rotation reload proven (step 2).

## Rejected alternatives

- **Advertise `turns:5349` (the "one-liner" stopgap a):** IMPOSSIBLE without patching
  LiveKit source (hardcoded 443). Killed by the Heat falsifier.
- **Patch LiveKit source to advertise 5349:** a fork to maintain across upgrades; and 5349
  is more firewall-blockable than 443 — worse coverage for the exact hostile networks this
  serves. Rejected.
- **Second VNIC (vs secondary IP on the existing VNIC):** more moving parts than Shape B's
  secondary-IP approach for no gain. Rejected in favor of B if B1 clears.
- **coturn as a standalone TURNS:443 server:** replaces LiveKit's embedded TURN entirely —
  larger change, re-introduces a component the media-companion standup deliberately used
  LiveKit-embedded to avoid. Out of scope.

## Fold — author self-pass findings (folded back)

- **F1 (correctness bug in MY OWN rollback plan — fixed above).** The cutover deletes the
  `turn` stub AND adds the global `layer4` block to the Caddyfile. Naive rollback ("swap
  the stock binary back + restart") leaves the NEW Caddyfile in place — and **stock Caddy
  does not understand `layer4`, so it fails to start → :443 is DOWN → chat + signaling +
  video all dark.** Rollback MUST restore BOTH artifacts: keep `/usr/bin/caddy.stock` AND
  `/etc/caddy/Caddyfile.stock`; rollback = restore both, `caddy validate` with the stock
  binary, `systemctl restart`. A one-artifact rollback bricks the box. This is the single
  most dangerous line in the plan and it was wrong in the first draft.
- **F2 (TLS-ALPN challenge self-sabotage — folded into §ACME).** The matcher
  `@turn tls sni turn.enspyr.co` matches EVERY ClientHello with that SNI — **including the
  `acme-tls/1` renewal challenge for turn.enspyr.co**, which it would route to LiveKit:5349,
  failing the challenge → the cert eventually expires → all TURNS dies. Mitigation is not
  optional: **tls-alpn-01 MUST be disabled for turn.enspyr.co (force HTTP-01 on :80)** as an
  explicit build step, OR the turn route must additionally NOT-match `alpn acme-tls/1` (harder
  with positive-only matchers). Build step 3 adds: assert Caddy issues turn's cert via HTTP-01.
- **F3 (cert-reload bounces ALL live media — named tradeoff, owner: Nick).** The cert-reload
  hook (task #3) restarts `livekit-server` when the served cert goes stale. A LiveKit restart
  **drops every live room** on the island, not just TURNS — a full media-plane bounce every
  ~60–90 days at renewal. Accepted cost IF the restart is scheduled in a low-traffic window
  and alarmed. Open mitigation (claim C2): does LiveKit hot-reload its TURN cert on SIGHUP
  without a full restart? If yes, prefer that over restart. Not a blocker; a named tradeoff.

## Claims to falsify (for Fold + Temper)

- **B1** — LiveKit embedded TURN can/can't bind TURN/TLS to a specific IP (gates Shape B).
- **A1** — caddy-l4 `listener_wrappers` passthrough preserves end-to-end client↔LiveKit TLS
  on the SAME Caddy that terminates the other SNIs (RESEARCH says yes; verify on the box).
- **A2** — `caddy validate` actually catches a malformed layer4 wrapper (claimed; test it).
- **C1** — cert-restart hook reliably makes LiveKit serve the renewed cert (task #3 must be
  proven, not assumed).
- **A3** — experimental caddy-l4 @v0.1.2 is stable enough for a prod front door; pin + a
  rollback binary is sufficient mitigation.
- **A4** — `?transport=tcp` TURNS raw passthrough to :5349 satisfies the client (TURNS =
  TURN-over-TLS-over-TCP; passthrough is TCP-level — RESEARCH says correct; the probe
  proves it at acceptance).
- **C2** — does LiveKit hot-reload its TURN cert on SIGHUP (avoiding the full-restart media
  bounce, F3)? If yes, the cert-reload hook should SIGHUP, not restart.

## Open variables (no silent TODOs)

- Exact caddy-l4 `proxy`/`route` Caddyfile syntax for v0.1.2 (README pins the version's
  grammar — confirm at build time; `caddy validate` gates it).
- Whether to localhost-bind :5349 in the same cutover or a follow-up (leaning same cutover).
- Go toolchain source on the build host (box has none; build on mac/scratch, ship binary).
- imagineering (~35 services) — explicitly OUT; a separate gated change after enspyr proves.
