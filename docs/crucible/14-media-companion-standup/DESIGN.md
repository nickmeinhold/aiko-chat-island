# DESIGN — Media companion standup (LiveKit SFU + managed TURN)

**Task:** #14 · **Crucible:** `docs/crucible/14-media-companion-standup/` · **Status:** re-Cast after Temper round 1 (unanimous REQUEST_CHANGES), pre-Temper round 2
**Depends on:** `RESEARCH.md`. **Round-1 temper ledger:** §9.

---

## 1. Problem

The self-hosted LiveKit SFU — which carries STUN/TURN inside it (pion/turn), so there is **no separate coturn** — is the one piece of the media path never brought under the island's drift-killing discipline. Verified on the boxes 2026-08-11:

| | imagineering (`chat.imagineering.cc`) | enspyr (`chat.enspyr.co`) |
|---|---|---|
| LiveKit image | `livekit/livekit-server:**latest**` | `livekit/livekit-server:**latest**` |
| TURN | enabled, **cert EXPIRED Jul 24** (static Apr-25, no renewal) | **no `turn:` block — zero relay** |
| Caddy | **container** (`caddy:2-alpine`), cert store in Docker volume `/data/caddy/…` | **systemd** (`User=caddy`), cert store host FS `/var/lib/caddy/.local/share/caddy/…` |
| LiveKit tenancy | **shared box infra**: `realm-token` mint, dreamfinder, lyra, tech-world, AITW (Firebase webhook + Redis) | **bare, island-dedicated** |

Consequence: relay-fallback video is **broken** on imagineering (dead cert) and **absent** on enspyr (no TURN).

**Scope of the claim (narrowed after temper — decided with Nick 2026-08-11).** This restores **generic relay fallback**: users behind **symmetric NAT and most home/mobile firewalls** who can't hold a P2P path but *can* reach TURN on UDP 3478 / TLS 5349. It does **NOT** claim to serve **hostile locked-down corporate networks** that permit only TCP 443 — that needs TURN-on-443 coexistence with Caddy (port 443 is Caddy's), which is explicitly **out of scope** here and deferred to a follow-up task if a real 443-only user appears. (Round-1 Carnot/Tesla catch: 5349 proves TURN works, not that it works for a 443-only user.)

## 2. The frame (re-anchored after temper): media is always box-plane

Round 1, all three adversaries: calling both boxes "companion modes" under `deploy/media/` still *island-shapes a box plane*. The truer frame:

> **The media plane (SFU + TURN) is always BOX-plane, never island-plane.** The island is one *consumer* of it.

Two operator postures on that box plane, drawn on the **ownership** line:

- **BOOTSTRAP (enspyr + any future greenfield island):** the box has no media plane yet, so we stand one up *for* the island. Repo-authoritative, island-owned lifecycle. Artifact: `deploy/media/` (pinned compose, `livekit.yaml.tmpl`, restart trigger, relay test). Because the box is island-dedicated, the island legitimately owns this plane's lifecycle — including an **unattended** renewal restart (the blip only hits island users).
- **CLIENT + REPAIR (imagineering):** a media plane already exists, shared by AITW/dreamfinder/lyra/token-mint, **owned outside the island**. The island is a *client* of it. We do the minimum to fix #14 — **pin the image, deliver the cert, alarm on expiry** — via an **attended runbook** an operator runs in a change window, never an autonomous island timer reaching into someone else's plane. Artifact: `docs/runbooks/imagineering-livekit-repair.md`. **Named shared-SFU owner: Nick (operator); change-control: attended window + tenant notice.**

Physically-split artifacts (not one dual-mode dir) so responsibility can't diffuse — Carnot's catch. `deploy/media/` is island-owned automation; the runbook is a human procedure against a plane the island doesn't own.

## 3. Shape

```
aiko-chat-island/
  deploy/media/                     # BOOTSTRAP (enspyr + future islands) — island-owned
    docker-compose.yml              # pinned livekit image; Caddy cert dir bind-mounted RO; redis only if OWN needs dispatch
    livekit.yaml.tmpl               # ${DOMAIN} ${NODE_IP} ${API_KEY} ${API_SECRET}; turn.cert_file → the RO mount path; narrow relay range
    standup.sh                      # render template, first-cert-before-turn-enable, open firewall, up -d, relay test
    update.sh                       # pull pinned image, backup-first, up -d, verify
    cert-restart.timer + .sh        # BOOTSTRAP ONLY: detect served-cert change → restart livekit; alarm on notAfter
    e2e_media_relay.py              # forced-relay acceptance test (exact observable — §4a)
    test/cert-tree-contract.sh      # contract test: fixture cert trees (host-FS vs docker-volume) → trigger fires or fail-closes
    README.md                       # frame: media is box-plane; BOOTSTRAP here, CLIENT+REPAIR in the runbook
  docs/runbooks/
    imagineering-livekit-repair.md  # CLIENT+REPAIR — attended: pin, cert bind-mount, restart-in-window, tenant notice, rollback
```

### 3.1 Cert lifecycle — FOLDED to bind-mount (coupling removed, not guarded)

Round-1 Tesla/Carnot FATAL: the copy-based mechanism C (Caddy holds cert A, LiveKit serves copy B) recreates the Jul-24 class — a silent watcher death or path drift leaves LiveKit serving an aging copy while Caddy's cert is fine. **Remove the coupling:** LiveKit reads Caddy's cert *directly*.

- **Caddy still issues + auto-renews** the `turn.<domain>` cert via stock **HTTP-01** (port 80 already reachable on both boxes — no custom build, no DNS creds). Honors Nick's "Caddy-managed" decision.
- **Bind-mount ONLY the leaf domain directory READ-ONLY into the LiveKit container** (Round-2 Tesla invariant — never the whole Caddy data volume, which holds the ACME account key adjacent; a LiveKit RCE / mis-mount must not reach it), and point `turn.cert_file`/`key_file` straight at the mounted path. The cert bytes on disk are then **always live** — no copy, no second key material, no half-write.
  - enspyr: mount host `…/certificates/acme-v02…/turn.enspyr.co/` (leaf dir) RO.
  - imagineering: share Caddy's Docker cert volume **at the leaf-domain subpath** RO into the livekit container.
- **The only reason to touch LiveKit on renewal is pion/turn's lack of hot-reload** (RESEARCH §1) → it must **restart** to pick up the renewed bytes. So the "watcher" collapses to a **restart-on-change trigger**. Its detector is NOT a disk-file read (see the alarm below).
- **Disk-live ≠ process-live — the alarm must probe the ENDPOINT, not the file (Round-2 Carnot + Tesla FATAL).** Bind-mount removes *disk* dual-SoT but not *process-memory* dual-SoT: pion/turn loads the cert into memory at boot, so after a renewal the mounted file is fresh while LiveKit still **serves the stale in-memory cert on :5349**. An `openssl x509 -in mounted.crt` check is green while the endpoint is stale — Jul-24 in runtime form. So the served-cert alarm (both boxes) **probes the TLS endpoint LiveKit actually presents on 5349 (with SNI), from outside the container**, and alerts on *that* `notAfter` (< N days) → a stale-serving process surfaces as an alarm regardless of what's on disk. Alert sink: the existing Telegram/notify path (§7 open var names it).

### 3.2 Restart = a real multi-tenant media interruption + a FORCED renewal loop

Round-1 Carnot/Tesla FATAL: the old "restart drops only in-flight ICE" claim is **false** — embedded pion/turn shares the SFU's Go process, so `docker restart livekit` tears down room state + SFU-anchored media for **every tenant**. **This design no longer assumes established media survives.**

- **Empirical gate:** before trusting *any* survival characterization, run a **live-room restart test** (a call up, restart livekit, measure what drops and reconnect time). Documented, not asserted. **Until it completes, the runbook assumes worst-case full media outage** for tenant-notice severity (Round-2 Tesla).

**The renewal loop must be FORCED, not calendar-hoped (Round-2 Carnot + Tesla FATAL — this is the exact class that produced the 18-day dead cert: "nothing owned the closed loop from renew→serve").** The asymmetry is on the *actor-compulsion* axis, not just ownership:

- **BOOTSTRAP (enspyr) — machine-forced.** A `systemd` timer, triggered by the **endpoint-probe alarm** (§3.1) detecting a served-cert change/staleness, restarts livekit. Unattended; blast is island-only. (Even island-only, a 3am restart mid-demo is real — an accepted product cost, not free.)
- **CLIENT+REPAIR (imagineering) — human-forced, as a closed state machine in the runbook**, NOT "operator restarts in a window":
  1. **Detect:** endpoint-probe alarm on served-cert `notAfter < N days` (N chosen so a full change-window fits before expiry).
  2. **Page:** a **role** (island on-call), not a person — Nick is current holder but the runbook names the role + paging path + a secondary, so a single human is not the SPOF.
  3. **Notice:** tenant-notice template (dreamfinder/lyra/AITW owners) + lead time.
  4. **Act:** restart livekit in the window; **re-run the §4a acceptance gate**; rollback digest on failure.
  5. **Escalate:** if no change-window is taken within `T` of the alarm, escalate (the alarm re-fires; missed-window is itself paged) — so "attended" cannot silently decay into an unowned loop.

  Restart is priced as a tenant outage throughout. The reserved alternative (hot-reload TLS terminator on 5349 → no SFU restart on renewal — §6) is *restart-decoupling*, to revisit only if this loop's operator cost proves the dominant ops tax.

### 3.3 Pin — v1.13.5, and pin is NOT a no-op

Replace `:latest` with **`livekit/livekit-server:v1.13.5`**. **Caveat (RESEARCH §2):** v1.12.0 + v1.13.1 changed TURN auth (TTL now required; no relay-to-private-IP by default). So "fix cert" and "pin" are **separate relay-tested gates** (§4). **Before pinning imagineering, enumerate the TURN-token issuers** (realm-token mint, clients, agents) — a TTL-required pin can green `e2e_media_relay.py` and still break an older token path not on the list (Tesla).

### 3.4 Exposure model — proven before the range is opened to non-test traffic

Round-1/2 Tesla/Carnot: authz ≠ abuse-resistance, and "unauth ALLOCATE fails" is *necessary, not sufficient*, to call a public UDP surface safe. Split the gates and prove **exposure** (B) before widening the aperture, distinct from **connectivity** (A, §4a):

**Exposure-acceptance (B) — all required before opening the range to non-test traffic:**
- **Unauthenticated `ALLOCATE` fails** (negative test).
- Credentials **short-TTL, LiveKit-issued** (the v1.13.1 TTL-required pin enforces this), never static TURN secrets in yaml. **UPDATE 2026-08-11 (cage-match #128):** the explicit `LIVEKIT_TURN_CRED_CMD` gate was removed as unwireable — session-bound TURN has no standalone cred to mint or inspect. The property is now **by construction**: the only TURN cred that exists is the short-TTL, LiveKit-issued, session-bound one the SFU hands a joined client, and gate A *exercises exactly that path* (a real livekit-rtc client relays through it). B3's config-invariant asserts the rendered turn block carries no static TURN secret key. Proven, not merely asserted — just not via a separate probe. **Ordering caveat (cage-match #128 r3, Tesla):** gate A runs at standup step 8, *after* the firewall opens at step 7 — so the credential-model safety guaranteeing the range is safe to open at step 7 rests on *construction* (session-bound TURN) + B1 (unauth ALLOCATE fails, pre-open), NOT on a pre-open cred-mint probe. Gate A *demonstrates* the session-cred relay works but does so post-open (it proves external reachability). If a pre-open credential proof is wanted, gate A can run against the box's own-IP hairpin at step 6 (signaling is Caddy:443, always open); deferred with the B3 behavioral-probe work.
- **No relay to private IPs** — pin `turn`'s RFC1918/link-local deny as a config **invariant on both boxes** (RESEARCH §2: v1.12 defaults to this, but the design pins it explicitly — an open ALLOCATE surface without it is SSRF-shaped blast radius).
- **Range bounds proven closed:** an external probe confirms ports *outside* 50000–60000 are closed (so a mis-set `relay_range` default can't leave the old 1024–30000 open from a prior snowflake).
- **Trust boundary on token minting:** the §3.3 issuer inventory (realm-token / clients / agents) is the *allowlist* of who may mint TURN-capable tokens — named + accepted, not just enumerated.
- **Abuse ceiling:** LiveKit/pion rate + allocation limits named and accepted-or-tightened (a leaked short-TTL token can still burn relay bandwidth until detection); allocation/traffic alarm on the range.

**Ordering invariant (Round-2 Carnot FATAL):** the credential negative test runs while exposure is still constrained (test-only firewall / source-restricted), *then* the range is opened to real traffic — never the reverse. §4 sequences this.

### 3.5 Firewall (double-firewall, per `reference_oci_double_firewall_local_iptables`)

Set **narrow** `turn.relay_range_start: 50000` / `relay_range_end: 60000` in `livekit.yaml` (default `1024–30000` — RESEARCH §5). Per box open **both** OCI security-list AND host iptables for **UDP 3478**, **TCP 5349**, **UDP 50000–60000**. Keep the SFU's own ICE UDP range (7882–7892) **disjoint** from the relay range — asserted as an invariant in the template. Verify with an *external* UDP probe.

## 4. Build order (core-first, each step independently useful)

1. **Fix the fire — cert only (imagineering CLIENT+REPAIR):** add `turn.imagineering.cc` Caddy block → Caddy issues via HTTP-01 → bind-mount into livekit → **attended restart in a window** → relay test on the CURRENT (`:latest`) version. Stops the 18-day bleed. *No pin yet.*
2. **Pin imagineering (separate gate):** after enumerating token issuers (§3.3), bump `:latest`→`v1.13.5`, attended restart, **re-run relay test** (catches v1.12/v1.13.1 TURN-auth changes). Rollback = re-pin prior digest.
3. **Author `deploy/media/` (BOOTSTRAP) + the CONSUME runbook:** pinned compose w/ RO cert mount, template (narrow relay range, disjoint ICE range), `cert-restart` timer, `e2e_media_relay.py`, the cert-tree contract test, README; and `docs/runbooks/imagineering-livekit-repair.md`.
4. **enspyr BOOTSTRAP standup:** `turn.enspyr.co` A-record (Namecheap API) → box; add `turn.enspyr.co` Caddy block; **obtain first cert BEFORE enabling TURN** (LiveKit won't boot with `turn.tls_port` + missing `cert_file`) — start turn-disabled, wait for cert, enable, restart; **pin the RFC1918 relay-deny + short-TTL cred config; run exposure-acceptance (B, §3.4) while the range is still test-only/source-restricted; ONLY THEN open the firewall range to real traffic**; **connectivity-acceptance (A, §4a) passes (parity)**.
5. **Wire the BOOTSTRAP restart trigger + endpoint-probe expiry alarm (both boxes):** enspyr timer (alarm-triggered restart); both-box served-cert `notAfter` **endpoint probe** (§3.1). Prove by forcing a renewal → restart → gate A still passes; and by pointing the probe at a deliberately-stale endpoint → alarm fires.
6. **Live-room restart measurement (§3.2):** document what a restart actually costs on the shared box, feeding the runbook's tenant-notice policy (default worst-case until this lands).
7. **imagineering CLIENT+REPAIR runbook dry-run:** walk the §3.2 state machine end-to-end once (detect→page→notice→restart→re-gate→escalate) so "attended" is proven executable, not prose.

## 4a. Acceptance gates — CONNECTIVITY (A) vs EXPOSURE (B), split (Round-2 Tesla)

Green connectivity ≠ safe to expose. **A proves the path exists; B (§3.4) proves it's safe to open.** Both required; A here, B in §3.4.

**A — connectivity acceptance.** `type == relay` alone is insufficient (a relay candidate can be embedded-TURN over UDP). Read the gathered candidates and assert:
1. **relay media round-trips (UDP path):** `iceTransportPolicy: 'relay'` (forbids host/srflx) → a real livekit-rtc client's synthetic video is received by a second client, **and every gathered ICE candidate is `candidate_type == relay`** (media had no path but the TURN allocation). Implemented by `deploy/media/e2e_relay_livekit.py`, gated by `e2e_media_relay.py` gate A. Proves the **UDP/3478** relay path.

> **UPDATE 2026-08-11 — the original assertion (1) below is DEFERRED; it does not hold today.** The gate was written to require the relayed path over **TLS/TCP (5349)**. Live testing proved that path **non-functional for clients**: LiveKit advertises only the UDP TURN (`turn.externalTLS:false`); a forced-relay client gathers a single UDP relay candidate, and with UDP blocked the peer connection times out (`wait_pc_connection`). The `:5349` cert is valid — the relay **advertisement** is the gap, an `external_tls` config matter tracked separately. Until that task proves TLS relay, gate A asserts the **UDP** path only (per (1) above), and the memory scope-note ("generic relay fallback for symmetric-NAT / home+mobile firewalls; hostile-443-only-corporate deferred") is the governing scope. The stale original text, kept for provenance:
>
> - ~~**relayed path over TLS/TCP works:** `iceTransportPolicy: 'relay'` forcing a TURNS URL → selected remote candidate `type == relay` **AND** its `protocol`/relay-transport resolves to **TCP/TLS**.~~ (deferred — TLS relay not advertised to clients; see UPDATE.)
> - ~~**UDP-relay canary:** a second run proving 3478 + the relay range relays over UDP.~~ (subsumed into (1) — the forced-relay client IS the UDP proof.)

## 4b. Degenerate states enumerated (author self-strike + round-1 neighbors)

- **First cert (enspyr):** LiveKit won't boot with `turn.tls_port` + missing `cert_file` → step 4 sequences cert-before-turn-enable.
- **Trigger dies silently:** covered by the served-cert `notAfter` alarm (§3.1) — a dead timer surfaces as an expiry alert, not a Jul-24 repeat.
- **Restart during active calls:** reclassified as real multi-tenant downtime (§3.2) — attended window on imagineering.
- **Pin is a behavior change:** steps 1 & 2 are separate relay-tested gates.
- **Caddy upgrade / volume rename moves the cert path:** the bind-mount path is asserted by the cert-tree **contract test** (§3, `test/`) against fixture trees for both box topologies; a moved path fails the test, not production.
- **relay range hole:** §3.5 narrows + opens explicitly; ICE range disjoint asserted in template.

## 5. Blast radius & consent spine

- **A LiveKit restart is a real media outage for ALL tenants** (§3.2), not just island users. **BOOTSTRAP (enspyr):** island-owned, unattended, island-only blast. **CLIENT+REPAIR (imagineering):** attended change window + tenant notice (dreamfinder/lyra/AITW) + rollback; the island's automation NEVER autonomously restarts imagineering's shared SFU. **Owner: Nick.**
- **Do not clobber imagineering's `livekit.yaml`** (API keys, webhook, redis are load-bearing for AITW). CLIENT+REPAIR touches only the cert mount + the image pin.
- **Public UDP surface gated by the credential model (§3.4)** — unauthenticated ALLOCATE must fail before the range is opened to non-test traffic; creds are short-TTL LiveKit-issued.
- **Secrets:** LiveKit API key/secret stay on the box (`.env`/rendered yaml), never in the repo template (`${VARS}`).

## 6. Rejected alternatives

- **Fold LiveKit into the island's pinned image** — coupling + multi-tenant contradiction.
- **Standalone coturn** — subtraction; embedded TURN is enough **iff media ≡ LiveKit-only on these boxes forever**. Two rejection axes (Round-2 Carnot: the *operational-decoupling* case is the real one, not "no media use-case"): (a) **media features** — no non-LiveKit WebRTC consumer exists; (b) **operational decoupling** — a separate TURN *could* renew/restart without dropping SFU rooms. We reject (b) too, but **route it explicitly to the reserved restart-decoupling trigger** (the hot-reload TLS terminator on 5349 below), which achieves the same decoupling without a second full TURN service to credential + harden. Reopen triggers (documented, not present debt): a non-LiveKit WebRTC consumer · a hostile-443 requirement wanting a dedicated 443 endpoint · **the imagineering attended-restart loop becoming the dominant ops tax** (→ build the terminator, still not coturn).
- **Cert mechanism A (custom `xcaddy` + `caddy-events-exec`)** — a hand-compiled Caddy is new forever-drift; module is experimental + permission-gotcha under enspyr's unprivileged Caddy.
- **Cert mechanism C-copy (fingerprint-diff copy+validate+restart)** — round-1 casualty: guards the coupling instead of removing it; bind-mount (§3.1) dissolves it.
- **Cert mechanism B (standalone `lego`/`certbot`)** — reverses Nick's "Caddy-managed" decision; bind-mount keeps Caddy as issuer while removing the copy, so B is unneeded. (Fallback only if the decision flexes.)
- **Hot-reload TLS proxy fronting 5349 (no restart on renewal)** — would zero out imagineering's tenant impact without operator involvement; held in reserve, heavier (more parts), revisit only if the attended-window cost proves unacceptable.

## 7. Open variables (build-time)

- **DNS:** confirm `turn.imagineering.cc` resolves + port 80 reachable for HTTP-01; **create `turn.enspyr.co` A-record** (Namecheap API).
- **imagineering Caddy cert-volume share:** exact RO mount of the Docker cert volume into livekit — pick at build.
- **Exact WebRTC-stats field** for the relay-over-TLS protocol assertion (§4a) — confirm against the SDK.
- **imagineering TURN-token issuer list** (§3.3) — enumerate before pin.
- **OWN-mode Redis?** enspyr single-node likely needs none — confirm before templating.

## 8. Claims to falsify (carried into Temper round 2)

1. **Frame:** does "media is box-plane; BOOTSTRAP vs CLIENT+REPAIR" hold, or does splitting the artifacts leave a seam? (round-1 frame finding folded — round 2 confirms.)
2. **Bind-mount** genuinely removes the dual-SoT class (vs the copy it replaced)?
3. **Restart reclassified honestly** — is "attended window + tenant notice on imagineering, unattended on enspyr" a real control plane now, or still folklore?
4. **Credential model + acceptance gate** — is the negative-test/exact-observable spec sufficient to call the public UDP surface safe?
5. **No coturn** still stands under the narrowed (non-443) scope?

## 9. Temper round-1 ledger (unanimous REQUEST_CHANGES → folded)

| # | Finding | Reviewer(s) | Disposition |
|---|---|---|---|
| 1 | "restart drops only in-flight ICE" is false (embedded TURN shares SFU process) | Carnot, Tesla (FATAL) | **Folded** §3.2 — reclassified as real multi-tenant outage; live-room test required |
| 2 | Copy-based cert = dual-SoT, recreates Jul-24 class | Tesla (FATAL), Carnot (MAJOR) | **Folded** §3.1 — bind-mount RO, watcher→restart-trigger, notAfter alarm |
| 3 | OWN/CONSUME half-dodge; island-shapes a box plane | all three (FRAME) | **Folded** §2 — media-is-box-plane; BOOTSTRAP vs CLIENT+REPAIR; artifacts split; owner named |
| 4 | 5349 ≠ 443; "corporate firewall" over-claimed | Carnot (FATAL) | **Folded** §1 — scope narrowed to generic relay (Nick decided); 443 deferred |
| 5 | Open relay before credential model | Tesla, Carnot | **Folded** §3.4 — negative test + short-TTL LiveKit creds precondition |
| 6 | Acceptance gate underspecified; needs exact observable + UDP canary + token matrix | Carnot, Tesla | **Folded** §4a, §3.3 |
| 7 | Two delivery topologies ≠ one script; need contract test | Tesla | **Folded** §3 `test/`, §4b |
| 8 | CONSUME "reference" goes stale | Kelvin, Tesla | **Folded** §2 — it's a runbook + named owner + change-control, not a passive doc |

### Temper round-2 ledger (Kelvin flipped APPROVE; Carnot + Tesla REQUEST_CHANGES on real, converged seams → folded)

| # | Finding | Reviewer(s) | Disposition |
|---|---|---|---|
| r2-1 | RESEARCH §1 still carries the false "ICE-only" restart claim §3.2 killed | Carnot, Tesla (FATAL) | **Folded** — RESEARCH §1 struck + marked SUPERSEDED |
| r2-2 | Served-cert alarm reads disk, not the process's in-memory cert → can't detect stale-serving (process-memory dual-SoT) | Carnot, Tesla (FATAL) | **Folded** §3.1 — alarm = TLS **endpoint probe** on :5349 w/ SNI, from outside the container |
| r2-3 | CLIENT+REPAIR renewal loop is folklore/"vibe", not a closed control plane; "nothing forces the actor" = the Jul-24 class | Carnot, Tesla (FATAL) | **Folded** §3.2 — closed state machine: detect→page(role)→notice→restart→re-gate→escalate-if-missed |
| r2-4 | Firewall opened before the credential negative test (ordering reverses the safety invariant) | Carnot (FATAL) | **Folded** §3.4 ordering invariant + §4 step 4 resequenced |
| r2-5 | Credential model necessary-not-sufficient; need private-IP deny, range-closed proof, issuer trust-boundary, abuse ceiling | Carnot, Tesla (MAJOR) | **Folded** §3.4 — exposure-acceptance (B) split from connectivity (A) |
| r2-6 | Mount whole Caddy volume exposes the ACME account key | Tesla (MAJOR) | **Folded** §3.1 — leaf-domain dir only, invariant |
| r2-7 | No-coturn rejects "no media use-case" but not the operational-decoupling case | Carnot (MAJOR) | **Folded** §6 — routes decoupling to the reserved terminator trigger |
| r2-8 | CRUCIBLE.md still leads "companion sibling" — package speaks two frames | Tesla (COMMENT) | **Folded** — CRUCIBLE restated to box-plane (seed kept historical) |
| r2-9 | Tenant-notice severity set before reconnect behavior known | Tesla (COMMENT) | **Folded** §3.2 — runbook defaults worst-case until step-6 measurement |
