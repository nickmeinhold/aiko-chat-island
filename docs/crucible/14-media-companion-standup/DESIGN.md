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
- **Bind-mount Caddy's cert dir READ-ONLY into the LiveKit container**, and point `turn.cert_file`/`key_file` straight at the mounted path. The cert bytes are then **always live** — no copy, no second key material, no half-write.
  - enspyr: mount host `/var/lib/caddy/.local/share/caddy/certificates/acme-v02…/turn.enspyr.co/` RO.
  - imagineering: share Caddy's Docker cert **volume** (or its host path) RO into the livekit container.
- **The only reason to touch LiveKit on renewal is pion/turn's lack of hot-reload** (RESEARCH §1) → it must **restart** to pick up the renewed bytes. So the "watcher" collapses to a **restart-on-change trigger**, nothing more:
  - **BOOTSTRAP (enspyr):** a `systemd` timer diffs the *served* cert's fingerprint (read from the running container's cert path) and, on change, restarts livekit. Unattended — the blip is island-only.
  - **CLIENT+REPAIR (imagineering):** **no timer.** The runbook has the operator restart livekit in a change window after a renewal, with tenant notice. (Renewals are ~monthly and LE warns ~30 days ahead, so this is a scheduled, not reactive, task.)
- **Expiry alarm on the SERVED cert (both boxes), not "diff exit 0"** (Tesla): a cron/systemd check reads `notAfter` from the cert LiveKit is *actually serving* and alerts if < N days — so a dead trigger surfaces as an alarm long before it becomes a Jul-24 repeat.

### 3.2 Restart = a real multi-tenant media interruption (blast-radius reclassified)

Round-1 Carnot/Tesla FATAL: the RESEARCH §1 "restart drops only in-flight ICE, not established media" claim is **almost certainly false** — embedded pion/turn shares the SFU's Go process, so `docker restart livekit` tears down room state + SFU-anchored media for **every tenant**. **This design no longer assumes established media survives.** Instead:

- **Empirical gate:** before trusting *any* survival characterization, run a **live-room restart test** — a call up on imagineering (or a staging room), restart livekit, measure what actually drops and how fast clients reconnect. The result is documented, not asserted.
- **Treat every renewal restart as scheduled media downtime:** BOOTSTRAP (enspyr) absorbs it unattended because it's island-only; CLIENT+REPAIR (imagineering) makes it an **attended window with tenant notice** (dreamfinder/lyra/AITW owners) + a rollback path. Restart is priced as an outage, not a nick.

### 3.3 Pin — v1.13.5, and pin is NOT a no-op

Replace `:latest` with **`livekit/livekit-server:v1.13.5`**. **Caveat (RESEARCH §2):** v1.12.0 + v1.13.1 changed TURN auth (TTL now required; no relay-to-private-IP by default). So "fix cert" and "pin" are **separate relay-tested gates** (§4). **Before pinning imagineering, enumerate the TURN-token issuers** (realm-token mint, clients, agents) — a TTL-required pin can green `e2e_media_relay.py` and still break an older token path not on the list (Tesla).

### 3.4 Credential model — precondition before opening the relay range

Round-1 Tesla/Carnot: opening 3478 + 5349 + a UDP relay range while `[confirm not open relay]` is still a TODO = shipping the hole before the credential model. **Falsify before build:**
- **Unauthenticated `ALLOCATE` must fail** — an explicit negative test.
- TURN credentials must be **short-TTL, LiveKit-issued** (the v1.13.1 TTL-required pin enforces this), never long-lived static TURN secrets in yaml.

### 3.5 Firewall (double-firewall, per `reference_oci_double_firewall_local_iptables`)

Set **narrow** `turn.relay_range_start: 50000` / `relay_range_end: 60000` in `livekit.yaml` (default `1024–30000` — RESEARCH §5). Per box open **both** OCI security-list AND host iptables for **UDP 3478**, **TCP 5349**, **UDP 50000–60000**. Keep the SFU's own ICE UDP range (7882–7892) **disjoint** from the relay range — asserted as an invariant in the template. Verify with an *external* UDP probe.

## 4. Build order (core-first, each step independently useful)

1. **Fix the fire — cert only (imagineering CLIENT+REPAIR):** add `turn.imagineering.cc` Caddy block → Caddy issues via HTTP-01 → bind-mount into livekit → **attended restart in a window** → relay test on the CURRENT (`:latest`) version. Stops the 18-day bleed. *No pin yet.*
2. **Pin imagineering (separate gate):** after enumerating token issuers (§3.3), bump `:latest`→`v1.13.5`, attended restart, **re-run relay test** (catches v1.12/v1.13.1 TURN-auth changes). Rollback = re-pin prior digest.
3. **Author `deploy/media/` (BOOTSTRAP) + the CONSUME runbook:** pinned compose w/ RO cert mount, template (narrow relay range, disjoint ICE range), `cert-restart` timer, `e2e_media_relay.py`, the cert-tree contract test, README; and `docs/runbooks/imagineering-livekit-repair.md`.
4. **enspyr BOOTSTRAP standup:** `turn.enspyr.co` A-record (Namecheap API) → box; add `turn.enspyr.co` Caddy block; **obtain first cert BEFORE enabling TURN** (LiveKit won't boot with `turn.tls_port` + missing `cert_file`) — start turn-disabled, wait for cert, enable, restart; open firewall; **credential negative test (§3.4)**; **relay test passes (parity)**.
5. **Wire the BOOTSTRAP restart trigger + expiry alarm (both boxes):** enspyr timer; both-box served-cert `notAfter` alarm. Prove by forcing a renewal → restart → relay test still passes.
6. **Live-room restart measurement (§3.2):** document what a restart actually costs on the shared box, feeding the runbook's tenant-notice policy.

## 4a. Acceptance gate (exact observable — Round-1 Carnot/Tesla)

`type == relay` alone is not enough (a relay candidate can be embedded-TURN over UDP). The gate reads the **selected candidate pair** and asserts:
1. **relayed path over TLS/TCP works:** with `iceTransportPolicy: 'relay'` forcing a TURNS URL, the selected pair's remote candidate `type == relay` **AND** its `protocol`/relay-transport resolves to **TCP/TLS** (exact WebRTC-stats field confirmed against the SDK at build time — not assumed).
2. **UDP-relay canary:** a second run proving 3478 + the relay range relays over UDP (so the opened range is provably necessary + reachable).
3. **negative test:** unauthenticated `ALLOCATE` fails (§3.4).

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
- **Standalone coturn** — subtraction; embedded TURN is enough **iff media ≡ LiveKit-only on these boxes forever**. Reopen trigger (documented, not present debt): a non-LiveKit WebRTC consumer, OR a hostile-443 requirement that wants a dedicated 443 TURN endpoint.
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
