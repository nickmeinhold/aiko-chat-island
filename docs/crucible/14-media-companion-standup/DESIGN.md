# DESIGN — Media companion standup (LiveKit SFU + managed TURN)

**Task:** #14 · **Crucible:** `docs/crucible/14-media-companion-standup/` · **Status:** Cast (pre-Fold, pre-Temper)
**Depends on:** `RESEARCH.md` (5 mechanism cells marked `[HEAT-PENDING]` below)

---

## 1. Problem

The self-hosted LiveKit SFU — which carries STUN/TURN inside it (pion/turn), so there is **no separate coturn** — is the one piece of the media path that never came under the island's drift-killing discipline. Live symptoms, verified on the boxes 2026-08-11:

| | imagineering (`chat.imagineering.cc`) | enspyr (`chat.enspyr.co`) |
|---|---|---|
| LiveKit image | `livekit/livekit-server:**latest**` | `livekit/livekit-server:**latest**` |
| TURN | enabled, **cert EXPIRED Jul 24** (static Apr-25, no renewal) | **no `turn:` block — zero relay** |
| Caddy | **container** (`caddy:2-alpine`), cert store in Docker volume `/data/caddy/…` | **systemd** (`User=caddy`), cert store host FS `/var/lib/caddy/.local/share/caddy/…` |
| LiveKit tenancy | **shared box infra**: `realm-token` mint, dreamfinder, lyra, tech-world, AITW (Firebase webhook + Redis) | **bare, island-dedicated** (no webhook, no Redis) |

Consequence: relay-fallback video is **broken** on imagineering (dead cert) and **absent** on enspyr (no TURN). Users behind symmetric NAT / restrictive firewalls cannot connect.

## 2. The frame decision (answers CRUCIBLE claim 1)

**The media plane is not one shape across the two boxes, so the design is not one application across them.** One repo artifact — a `deploy/media/` companion standup — applied in **two modes**:

- **OWN (enspyr + any future greenfield island):** the island *ships with* the media companion as its sibling. Repo-authoritative `livekit.yaml` + pinned compose + cert hook + standup/update + relay test. This is the literal "companion standup sibling to the island."
- **CONSUME + minimally repair (imagineering):** LiveKit here is shared box infra the island *consumes*, owned outside the island's lifecycle. The companion is a **documented reference**, NOT auto-applied. We touch only the two things that fix #14 without clobbering other tenants: **(a) pin the image**, **(b) add the `turn.imagineering.cc` Caddy cert block + renewal hook.** We do **not** overwrite imagineering's multi-tenant `livekit.yaml` (its webhook/redis/keys stay verbatim).

This is exactly task #9's hint ("capture the enspyr SFU hand-build as the reference") and #14's open question ("consider whether the SFU should become a documented companion standup"). It rejects both poles the CRUCIBLE named: not folded into the island image (coupling), not pretending both boxes are symmetric (breaks imagineering's tenants).

## 3. Shape

```
aiko-chat-island/
  deploy/media/                     # the companion standup (NEW)
    docker-compose.yml              # pinned livekit image + redis (redis only where OWN mode needs dispatch)
    livekit.yaml.tmpl               # OWN-mode template; ${DOMAIN} ${NODE_IP} ${API_KEY} ${API_SECRET}
    standup.sh                      # OWN-mode: render template, open firewall, first cert, up -d, verify
    update.sh                       # pull pinned image, backup-first, up -d, verify /health + relay
    cert-renew-hook.sh              # Caddy-triggered: copy renewed turn cert → livekit path → restart
    e2e_media_relay.py              # forced-relay acceptance test (iceTransportPolicy: relay)
    README.md                       # OWN vs CONSUME modes; imagineering = CONSUME reference
    caddy/
      turn.imagineering.cc.block    # snippet to add to imagineering's container Caddyfile
      turn.enspyr.co.block          # snippet to add to enspyr's systemd Caddyfile
```

### 3.1 The cert lifecycle (the load-bearing mechanism) — FOLDED to mechanism C

The trick: **Caddy owns the cert; LiveKit only reads it.** A plain `turn.<domain>` block makes Caddy obtain + auto-renew the LE cert via the **port-80 HTTP-01 challenge** (port 80 is already reachable on both boxes — no DNS-API creds, no custom Caddy build) — completely independent of TURN's UDP-3478 / TLS-5349 media traffic. Then a **standalone renewal watcher** delivers the cert bytes to LiveKit's `turn.cert_file` path and restarts LiveKit (pion/turn loads cert at startup only — **no hot-reload**, livekit#3463, so renewal ⇒ restart; restart drops only in-flight ICE, not established media).

**Why NOT Caddy's own on-renewal hook (the research's default):** that needs a *custom `xcaddy` build* with `caddy-events-exec` (experimental, background-exec, permission-denied under enspyr's unprivileged `User=caddy`) — building a new drift surface (a hand-compiled Caddy to re-version forever) to kill an old one. Remove the coupling, don't guard it. Caddy still *issues + renews* the cert (honors the "Caddy-managed" decision); a tiny external watcher does the delivery.

**The watcher (one script, per-box path adapter):** a `systemd` timer (daily) that:
1. locates Caddy's live `turn.<domain>.{crt,key}` — enspyr host FS `/var/lib/caddy/.local/share/caddy/certificates/acme-v02…/turn.enspyr.co/`; imagineering Docker volume (`docker cp` / mounted volume from `/data/caddy/…`).
2. compares its SHA-256 to the cert currently deployed at LiveKit's `turn.cert_file`. **No change → exit (no restart).**
3. on change: **validate the new crt+key pair** (`openssl` modulus match + not-expired) — **fail-closed: if it doesn't validate, do NOT restart** (keep serving the still-valid old cert until next cycle; a soon-to-expire cert beats a broken TURN).
4. atomic-copy (write-temp + rename) into LiveKit's cert dir → `docker restart livekit`.

Change frequency ≈ monthly (LE 90-day certs renew at ~30 days left), so the restart is rare and can be pinned to a low-traffic hour on multi-tenant imagineering.

### 3.2 Pin — v1.13.5, and pin is NOT a no-op

Replace `:latest` with **`livekit/livekit-server:v1.13.5`** on both boxes. **Caveat (RESEARCH §2):** v1.12.0 + v1.13.1 changed TURN auth (TTL now required; no relay-to-private-IP by default). If a box's current `:latest` predates v1.12, pinning **can change TURN behavior** — so the build order treats "fix cert" and "pin" as *separate verified steps*, each followed by the relay test. imagineering's pin is applied in place (CONSUME); enspyr's lives in the repo compose (OWN).

### 3.3 Firewall (double-firewall, per `reference_oci_double_firewall_local_iptables`)

Set a **narrow** `turn.relay_range_start: 50000` / `relay_range_end: 60000` in `livekit.yaml` (default is a huge `1024–30000` — RESEARCH §5), then per box open **both** OCI security-list AND host iptables for: **UDP 3478** (TURN), **TCP 5349** (TURN/TLS), **and UDP 50000–60000** (relay allocation). Keep this distinct from the SFU's own ICE UDP range (7882–7892). Verify with an *external* UDP reachability probe, not the OCI API's `AVAILABLE` state.

## 4. Build order (core-first, each step independently useful)

1. **Fix the fire — cert only (imagineering CONSUME):** add `turn.imagineering.cc` Caddy block → Caddy issues a fresh cert → copy to LiveKit certs → restart → **run the relay test on the CURRENT (`:latest`) version.** Independently shippable; stops the 18-day bleed. *No pin yet* — prove relay works before changing the version.
2. **Pin imagineering (separate verified step):** bump `:latest` → `v1.13.5`, restart, **re-run the relay test** (catches any v1.12/v1.13.1 TURN-auth behavior change). Rollback = re-pin to the prior digest if relay regresses.
3. **Author the companion (repo):** `deploy/media/` — pinned compose, `livekit.yaml.tmpl` (with narrow relay range), `cert-watch.sh` + `.timer`, `e2e_media_relay.py`, README (OWN vs CONSUME).
4. **enspyr OWN standup:** create `turn.enspyr.co` A-record (Namecheap API) → box; add `turn.enspyr.co` Caddy block; **obtain the first cert BEFORE enabling TURN** (LiveKit won't boot with `turn.tls_port` + a missing `cert_file`) — start LiveKit turn-disabled, wait for cert, render template with `turn:` enabled, restart; open firewall; **relay test passes on enspyr (parity achieved).**
5. **Wire the renewal watcher both boxes:** install `cert-watch.timer`. Prove by simulating a cert swap (or forcing renewal) → watcher validates → restarts → relay test still passes.
6. **Backfill imagineering into the reference:** document its shared-infra config (webhook/redis/keys) as the CONSUME reference in the README; do NOT migrate it to the template.

## 4a. Acceptance gate (the real one)

Forced-relay test (`iceTransportPolicy: 'relay'`) is **not** passed by "video appeared" — a `relay` candidate can be the media node's embedded TURN over **UDP**, leaving the TLS/5349 path unproven. The gate is: selected candidate pair `type == relay` **AND `protocol == TCP/TLS`** (RESEARCH §6, livekit#3971). `e2e_media_relay.py` must assert the protocol field, not just the type.

## 4b. Fold — degenerate states enumerated (author self-strike)

- **n=0 / first cert (enspyr):** LiveKit fails to boot if `turn.tls_port` is set but `cert_file` is absent → **step 4 sequences cert-before-turn-enable.**
- **Renewal race:** watcher copies a half-written cert mid-renewal → mitigated by Caddy's atomic write + the watcher's own validate-then-atomic-copy; **fail-closed skips restart on an invalid pair.**
- **Restart during active calls (imagineering multi-tenant):** only in-flight ICE drops (established media survives); restart fires **only on actual cert change** (~monthly) at a low-traffic hour — not on every timer tick.
- **Pin is a behavior change, not a no-op:** v1.12/v1.13.1 TURN-auth changes → **steps 1 and 2 are separate relay-tested gates**, never bundled.
- **relay range hole:** forgetting `relay_range_*` → default `1024–30000` firewall hole; §3.3 narrows + opens explicitly.

## 5. Blast radius & consent spine

- **imagineering LiveKit restart drops live calls for OTHER tenants** (dreamfinder/lyra/AITW), not just the island. Cert-renewal restarts must be scheduled low-traffic; the fix-the-fire step-1 restart is a one-time, announce-able event. **Owner:** Nick (operator). This is the strongest argument for CONSUME mode — we minimize how often the island's tooling restarts a service other people depend on.
- **Do not clobber imagineering's `livekit.yaml`** (API keys, webhook, redis are load-bearing for AITW). CONSUME mode touches only the cert + the image pin.
- **No new public surface** beyond TURN's already-intended UDP 3478 / TCP 5349 + relay range. TURN relays media for *authenticated* LiveKit sessions (it uses LiveKit's API-key credential flow), so it is not an open relay — `[HEAT-PENDING: confirm LiveKit embedded TURN requires the room credential, i.e. not an abusable open TURN]`.
- **Secrets:** LiveKit API key/secret live on the box (`.env` / rendered yaml), never in the repo template (template carries `${VARS}`).

## 6. Claims to falsify (carried into Fold + Temper)

Verbatim from CRUCIBLE + refined:
1. **FRAME** — is "companion" right, or is imagineering purely box-infra the island consumes? → **Resolved to a two-mode design (OWN/CONSUME); Temper must confirm the split is honest, not a dodge.**
2. **Asymmetry** — one mechanism must survive both box shapes → the two cert-delivery adapters; Temper: is a single hook script maintainable across container-vs-systemd Caddy, or should they be two scripts?
3. **Cert delivery across the container boundary** — `[HEAT-PENDING #2]`.
4. **pion/turn no hot-reload → restart on renewal** — `[HEAT-PENDING #1]`; blast radius §5.
5. **No coturn** — subtraction; Temper must show a LiveKit-embedded-TURN gap to overturn.

## 7. Rejected alternatives

- **Fold LiveKit into the island's pinned image** — coupling + multi-tenant contradiction (CRUCIBLE §Rejected).
- **Standalone coturn** — subtraction (claim 5).
- **Do nothing** — status quo that produced the dead cert.
- **Cert mechanism A: custom `xcaddy` + `caddy-events-exec` (research default)** — rejected in Fold: a hand-compiled Caddy binary is a *new* drift surface to re-version forever, the exec module is experimental with a permission gotcha under enspyr's unprivileged Caddy, and DNS-01 needs Namecheap creds + IP allowlist we don't need. More coupling to guard, not less.
- **Cert mechanism B: standalone `lego`/`certbot` (fully decouple from Caddy)** — cleaner in the abstract (TURN cert is a LiveKit concern), but adds a *second* ACME client to the box and **reverses Nick's "Caddy-managed" decision**. Chosen mechanism C keeps Caddy as issuer (honors the decision) while dropping only the custom-build cost. *(If Temper prefers full decoupling, B is the fallback — flag to Nick, since it changes the recorded decision.)*
- **Front TURN TLS with a hot-reloading proxy to avoid the restart** — more moving parts than a rare scheduled restart; re-examine only if restart blast radius proves unacceptable.

## 8. Open variables (Heat-resolved / remaining)

Resolved by RESEARCH: no hot-reload → restart (§1); pin `v1.13.5` + behavior-change caveat (§2); mechanism C via HTTP-01 stock Caddy (§3); narrow relay range `50000–60000` (§5); protocol-check acceptance gate (§6).

**Remaining (for Blade / build-time):**
- **DNS:** confirm `turn.imagineering.cc` resolves to the box (yaml references it — likely yes); **create `turn.enspyr.co` A-record** via Namecheap API.
- **Port 80 reachability** for `turn.<domain>` HTTP-01 — Caddy listens `:80` on both boxes; confirm no upstream block on the `turn.` host specifically.
- **imagineering cert-store host access:** the watcher needs to read Caddy's cert from the Docker volume (`docker cp` vs host-mounted volume) — pick at build time.
- **Does the OWN-mode compose need Redis?** enspyr's LiveKit is single-node (no agent dispatch) → likely no Redis; confirm before templating.
