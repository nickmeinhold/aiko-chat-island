# Off-:443 empirical rehearsal — results (task #6, 2026-08-12)

Four cage-match rounds hardened this artifact by argument. This is the first time the four
coupled state machines have actually **moved together while someone watched**.

Rig: a disposable Lima VM (Ubuntu 24.04.4, same as enspyr), real systemd, real iptables +
netfilter-persistent, real `docker` host-networked `livekit/livekit-server:v1.13.5`, real Caddy
2.11.4, real HAProxy 2.8.16, and a **real ACME server** (Pebble + dnsmasq) so cert issuance and
renewal exercise the genuine HTTP-01 path rather than `tls internal`. Harness:
`provision.sh` / `drive.sh` / `checks.sh` / `reset.sh` / `faults.sh`.

## Verdict

The cutover artifact **survived every test run against it**, including four reboot-at-checkpoint
runs, five rollback runs, and three fault injections. Two findings came out of it — one of them
in the *acceptance gate*, not the cutover.

`cutover.sh` may now legitimately be run with `OFF443_PROVEN=1` on enspyr, **after F1 is
resolved** (F1 is the gate that would tell you the cutover failed).

## What was proven

| Invariant | Result | Evidence |
|---|---|---|
| **INV-10** relay-deny survives the flip | ✅ | `awk` against production's real `livekit.yaml`: 2-out/1-in diff, `deny_peer_cidrs` intact, all other sections byte-identical, re-parses. |
| **INV-2** one owner of `:443`, bounded window | ✅ | Three full cutovers; dark window **96 / 97 / 100 ms**. Never two owners. |
| **INV-3** every intermediate state is boot-correct | ✅ | Reboot at CP1, CP2, CP3, CP4 and at the completed state. All five booted to exactly one `:443` owner, no public plaintext, chat up. |
| **INV-1** plaintext TURN never publicly reachable | ✅ | External vantage (container over `docker0`, so `! -i lo` applies) — **probe validated POSITIVE at CP0 first** (sees `:5349` OPEN), then `closed` at every post-firewall state, including across reboots. |
| **INV-8** Caddy `:8443` not publicly reachable | ✅ | Same vantage, `closed` at every state where `:8443` is bound. |
| **INV-5** real client IP preserved | ✅ | Request from the external container returns `x_forwarded_for: 172.17.0.2` — the container's own IP. Without PROXY protocol this collapses to `127.0.0.1` and the gateway's per-IP rate limiter with it. |
| **INV-6** rollback restores all four, idempotently | ✅ | Rollback from CP1/CP2/CP3/CP4 and from the completed cutover: both config artifacts `sha256` **byte-identical** to pre-cutover, firewall reopened, LiveKit back on TLS, HAProxy disabled. Second consecutive run exits 0 and changes nothing. |
| **INV-4** cert renewal reaches what HAProxy serves | ✅ | Forced a genuine ACME re-issue; `:443` kept serving the old cert until sync ran, then served the new one. |
| **INV-4** reload-failure is survivable (the r4 P0) | ✅ | With a deliberately broken `haproxy.cfg`: sync exits non-zero, **PEM kept** (valid cert + valid key), `.needs-reload` sentinel dropped, HAProxy still running. Restore config → `haproxy -c` OK (bootable) → next tick retries the owed reload, clears the sentinel, serves the current cert. |
| **INV-9** no false green on a dead LiveKit | ✅ | Injected an unbootable `livekit.yaml`: cutover distinguished "socket dead" from "plaintext", refused, and auto-rolled-back. Rollback then **hard-gated** — it refused to reopen the firewall because `:5349` wouldn't present TLS, choosing "TURN dead but closed" over "reopen and maybe expose plaintext." |
| **INV-7** SNI routing | ✅ | turn SNI → terminator cert; chat SNI → passthrough cert; unknown SNI → **byte-identical to Caddy's own answer** on `:8443` (differential test, not string-matching); non-TLS junk dropped; a rejected handshake does not poison the acceptor. |
| **The thesis** — TURN-over-TLS on `:443` actually works | ✅ | `B3` through the mux: `tls:turn.enspyr.co:443` → `allocated relay ['192.168.5.15', 54020]`, with `10/8`, `172.16/12`, `192.168/16`, `169.254.169.254`, `127.0.0.1` and `100.64.0.1` all **403 on that path**, public control `1.1.1.1` allowed before *and* after. |

Also confirmed empirically: LiveKit really does advertise
`turns:turn.enspyr.co:443?transport=tcp` — the premise the whole design rests on — and a missing
turn PEM makes `haproxy -c` exit with *"Fatal errors"*, i.e. the r4 bug would have made HAProxy
**unable to start at all**, not merely serve a stale cert.

## Findings

### F1 — the acceptance gate fails OPEN on the one thing it exists to check (HIGH)

`b3_relay_probe.py` returned **`result: OK`, exit 0** on a run where
`tls:turn.enspyr.co:443` was **unreachable** — it classified the TLS endpoint as
`"unreachable": [...]` ("not a relay vector, surfaced") and certified the run on the strength of
the UDP endpoint alone.

The RUNBOOK designates this probe as the post-cutover acceptance gate — *"must flip `turns:443`
UNREACHABLE → ALLOCATED"* — but **the probe does not assert that**; it prints it. So a cutover
that leaves TURN-over-TLS-on-443 completely dead (exactly the failure task #4 exists to fix)
would be green-lit by its own gate, and the only thing standing between that and a false "done"
is a human reading a JSON blob.

This is the same fail-open class rounds 1–2 of the #129 cage-match caught, one layer up: the
probe is sound as a *security* assertion (does the relay deny RFC1918) and fails open as a
*liveness* assertion (does the relay exist).

**FIXED** 2026-08-12 on `feat/b3-behavioral-probe` (`1cdf35a`): opt-in `B3_REQUIRE_ENDPOINT`
(`<transport>[:<host>][:<port>]`, `*` wildcards, matched on parsed fields so an IPv6 host can't
be mis-split) BLOCKs when a pinned endpoint is absent from `tested`. Security semantics
untouched. RED/GREEN proven in this rig with the guard as the only variable, against a
`turns:443` that fails exactly as the live islands do today (`TimeoutError`):

| rig state | guard | verdict |
|---|---|---|
| `turns:443` dead | off | `OK` / exit 0 — **the bug, reproduced** |
| `turns:443` dead | on | `BLOCK` / exit 2 |
| `turns:443` live | on | `OK` / exit 0 — no false-blocking |

Plus 10 unit cases on the matcher. Standup deliberately does **not** pin it (turns:443 is dead
on both islands until task #4 lands; pinning there would block every standup on a known gap).

### F2 — a SIGKILL between 2.3 and 2.4 leaves `:443` unbound with no watchdog (MEDIUM)

Freezing the system at CP3 (the dark window) shows `:443 UNBOUND, chat NO` — normally a 96 ms
transient. A **reboot** from there self-heals correctly (proven: CP3 post-reboot →
`haproxy:443 + caddy:8443`, chat up), because 2.3 enables the unit *before* the Caddy reload.
But a hard kill of `cutover.sh` at that instant is **not** covered: HAProxy is enabled and not
started, and nothing will start it. The outage persists until a human acts.

One command recovers it (`systemctl start haproxy`). Nobody had written that command down.
**Fix:** name it in the RUNBOOK's failure section (done in this commit).

### F3 — rollback restores what *was there*, not something known-good (LOW, by design)

In the INV-9 fault the `.stock` backup faithfully captured the already-corrupt `livekit.yaml`,
so rollback restored a LiveKit that still could not boot — and then correctly refused to reopen
the firewall. This is right, but it means "rollback succeeded" ≠ "the service is healthy".
Worth stating plainly in the RUNBOOK so nobody reads a clean rollback as a clean system.

## Honest scope of this rehearsal

- **arm64, not x86_64.** State-machine sequencing is architecture-independent; binary-level
  behaviour of the daemons is not strictly proven for the box's arch.
- **No public IP.** "Externally reachable" means *from a container over `docker0`* — a genuine
  non-loopback path that the `! -i lo` rules govern, but not the internet. The RUNBOOK's
  off-box `openssl s_client <public-ip>:5349` closure proof is still **mandatory** at cutover.
- **One documented artifact delta:** both Caddyfiles get `acme_ca`/`acme_ca_root` (and the turn
  block gets `dir`/`trusted_roots`) pointed at Pebble. Everything structural under test —
  `https_port 8443`, the `proxy_protocol` listener wrapper, `disable_tlsalpn_challenge`, the
  retained turn block — is the shipped artifact verbatim, and both substitutions are asserted.
- **The rig's LiveKit is on a private network**, so "allocate to a public peer" is only exercised
  as a control-plane permission (`1.1.1.1` → 200), not as real relayed traffic.
- `cutover.sh` gained a `ckpt()` hook to stop at a checkpoint. It is double-gated behind
  `REHEARSAL=1` **and** a named `CUTOVER_STOP_AFTER`, and is inert in production.

## Re-running it

```bash
limactl start --name=turnrig --cpus=2 --memory=4 --disk=20 template://ubuntu-24.04
limactl shell turnrig -- sudo VM_IP=<guest-ip> bash provision.sh
./drive.sh sync && ./drive.sh baseline
./drive.sh reboot CP1|CP2|CP3|CP4|done
./drive.sh rollback CP1|CP2|CP3|CP4|done
./drive.sh fault-livekit
./drive.sh full && limactl shell turnrig -- sudo bash /tmp/faults.sh
```

Note: Pebble mints a **new issuance root on every container start**, so after any guest reboot,
refresh `/usr/local/share/ca-certificates/pebble-issuance-root.crt` from
`https://127.0.0.1:15000/roots/0` and re-issue the site certs, or TLS verification will fail for
reasons that have nothing to do with the artifact under test.

---

# Task #8 Phase-0 falsifier — plain SNI passthrough (2026-08-14)

**Question.** `external_tls: true` is the choice that creates every plaintext-window invariant
in the cutover (INV-1 and the firewall DROPs, the ordering constraint, the `livekit.yaml`
mutation, `haproxy-cert-sync` and its PEM uid boundary — the round-4 P0). It buys exactly one
thing: avoiding a LiveKit restart on cert renewal, ~4x/year. The alternative is **plain SNI
passthrough** — HAProxy peeks the ClientHello and forwards the raw stream to LiveKit's own TLS
TURN listener, holding **no certificate at all**. The design flagged one risk against it and
never tested it: *"passthrough that works for `openssl s_client` but not for a real TURN client;
ClientHello fragmentation."*

**Verdict: GREEN.** Passthrough carries a real call.

| run | UDP TURN | selected pair | bytes | exit |
|---|---|---|---|---|
| **verdict** | blocked | `relay/tls` — `turns:turn.enspyr.co:443?transport=tcp` | 11429 ↑ / 1925 ↓ | **0** |
| **control** | allowed | `relay/udp` — `turn:203.0.113.5:3478` | 11428 ↑ / 1862 ↓ | **3** (correctly FAILS) |

The control is what makes the green mean anything: the same harness, same box, one variable
flipped, and it refuses to certify the UDP path. A green with no discriminating control is a
rubber stamp.

Corroborating server-side evidence, which is stronger than the client's own report:

- **HAProxy log**: `fe443 be_turn/lk` for Chromium's connections — the ClientHello was
  SNI-peeked and routed to the passthrough backend. The fragmentation risk is closed by
  observation, not inference.
- **B3 relay-deny through the passthrough**: `tls:turn.enspyr.co:443` ALLOCATED, all five
  SSRF sentinels 403, CGNAT 403, public control 200 before *and* after, on the same allocation.
  The security property survives the shape change.

**Scope limit (stated, not buried).** The rig's certs come from a local Pebble CA, so the
Chromium run used `--ignore-certificate-errors`. It therefore proves **transport**, not chain
validation. Chain validation is proven separately in production (PR#131, real Let's Encrypt
cert), and LiveKit reads that same Caddy-issued cert through the `/opt/turncerts` symlink — so
no *new* cert-plumbing is introduced by passthrough.

## Two instrument findings — both would have been reported as server bugs

1. **`--ignore-certificate-errors-spki-list` does NOT reach Chromium's TURNS socket.** With the
   Pebble root pinned by SPKI, `pion.turn` logged `TLS handshake failed: remote error: tls:
   unknown certificate` — Chromium rejecting the cert. Adding the root to the NSS user db
   (`~/.pki/nssdb`) did not help either; only the blanket flag did. Pin-by-SPKI silently does
   not apply here, and the failure is indistinguishable from a broken server unless you read
   the server log.

2. **The rig structurally cannot complete relay-only WebRTC as originally built.** Its SFU
   advertises `192.168.5.15` — RFC1918 — which the TURN relay correctly **refuses** as a peer
   (`deny_peer_cidrs`, `requestsSent: 8 / responsesReceived: 0`). That is the SSRF guard working
   against the SFU itself, and it fails identically under Shape C. Read without the server log
   it looks exactly like "passthrough is broken".

   **Fix, and it makes the rig permanently more capable:** alias a non-private address on `eth0`
   and point `rtc.node_ip` at it, so the relay is permitted to deliver to the SFU:

   ```bash
   ip addr add 203.0.113.5/32 dev eth0        # TEST-NET-3, allowed by the deny list
   sed -i 's/^  node_ip: .*/  node_ip: 203.0.113.5/' ~/apps/livekit/livekit.yaml
   docker restart livekit
   ```

   With that, the rig can run the real-client relay proof end to end — which is what task #11
   (wire the relay proof into a routine gate) needs a home for.

**Consequence for the design.** `external_tls` is a choice, not a requirement. Dropping it
deletes `haproxy-cert-sync.{sh,service,timer}`, the PEM uid boundary, the `.needs-reload`
sentinel, INV-1 and its v4+v6 DROPs, the `netfilter-persistent` dependency, the `livekit.yaml`
mutation, and the ordering constraint that shapes the entire state machine. What replaces it is
`cert-restart.sh`, which already exists and is already cage-matched.

---

# Passthrough shape — full rig rehearsal (2026-08-14, task #8 build)

The falsifier above proved passthrough *can* carry a call. This is the shipped artifact running
the whole state machine on the rig.

## Verdict

| test | result | evidence |
|---|---|---|
| full cutover | ✅ | `:443` dark window **90 / 91 / 94 / 95 ms** across four runs; `pass=5 fail=0`; success asserted separately (haproxy owns `:443`) |
| B3 acceptance gate, `tls:*:443` pinned | ✅ | ALLOCATED through the mux; 5 SSRF sentinels + CGNAT 403; public control 200 before *and* after |
| real Chromium relay proof through the mux | ✅ | selected pair `relay/tls`, **11425 B ↑ / 1860 B ↓**, UDP blocked so `:443` was the only path |
| rollback from `done` and `CP4` | ✅ | Caddyfile **byte-identical** to pre-cutover; **`livekit.yaml` byte-identical too** (it is never touched); second run exits 0 and changes nothing |
| reboot at `done` | ✅ | boots to exactly one `:443` owner (haproxy), Caddy on `:8443`, chat served |
| fault injections | ✅ | **17/17** — see below |

## Three fail-opens found, all in instruments rather than in the shape

The shape itself behaved. Everything that went wrong today was a *measuring* device, which is
the third session running where that has been the dominant failure mode.

### 1. The harness certified a cutover that never happened

`cutover.sh` correctly refused to run (no `TURN_DOMAIN`), and `drive.sh` reported
**`post-cutover ... RESULT pass=5 fail=0`** — over a box still at CP0.

`assert_safety` answers *"is this state safe"*, which is deliberately independent of *"did the
thing we ran work"*. Conflating them is exactly the F1 class from the last session, one layer
up: the gate certifying the outcome it exists to detect. Fixed by asserting **success**
separately (`assert_muxed` / `assert_unmuxed`, by `:443` ownership) and by refusing to report a
state at all when the cutover exited non-zero.

### 2. A green board over a media plane that could not work

After the reboot test the safety matrix reported `pass=5 fail=0` — while LiveKit was advertising
`203.0.113.5`, an address the box **no longer held** (the `ip addr add` alias did not survive
the reboot). Every listener, cert and route check was green; every relay call would have failed.

Ports and certs are not the media path. Added an **ADVERTISED-IDENTITY-IS-HELD** check — the
SFU's `node_ip` must be an address the box actually has — and RED/GREEN-proved it with the alias
as the only variable (`fail=1` without, `pass=6` with). The alias is now a persisted unit.

This is task #11's thesis demonstrating itself, unprompted, on the rig.

### 3. My own assertion encoded an assumption the mechanism does not make

FAULT 1 initially failed on *"after cert-restart, :443 serves the NEW cert"*. `cert-restart.sh`
is a **staleness** guard, not a renewal detector: it fires only once the *served* cert falls
inside `ALARM_NOTAFTER_DAYS` (14). Caddy renews at ~30 days remaining, so a renewed cert can sit
on disk unserved for **~16 days**.

Not a bug — that is the anti-thrash design, and the served cert stays valid throughout. But the
INV-4′ wording ("a renewal reaches what clients see") implied *promptly*, and `haproxy-cert-sync`
genuinely was prompt. The claim is now stated at its true scope, and the fault drives **both**
branches: no restart while fresh, propagation once stale (`served cert advanced
1794476582 → 1794476660`, watermark confirmed).

## Fault injections — 17/17

- **FAULT 1** cert renewal: no-thrash while fresh · re-issue produces a different cert · clients
  still see the old one (the hazard is real, not theoretical) · propagation once stale.
- **FAULT 2** cutover refuses a dead passthrough backend: aborts non-zero · says *why* · mutates
  **nothing** (Caddy still owns `:443`, no `.stock` staged) · re-cutover succeeds once healthy.
- **FAULT 3** SNI matrix: turn SNI → a cert that is **byte-identical to LiveKit's own on :5349**
  (the positive proof that this is true passthrough, not something HAProxy synthesised) · chat
  SNI → Caddy · unknown SNI → byte-identical to Caddy's own answer · a rejected handshake does
  not poison the acceptor · non-TLS junk dropped.

## A contract I nearly broke

The Phase-4 gate was first written as "`cert-restart.timer` must be active". `cert-restart.service`
carries an explicit constraint: *BOOTSTRAP-only — island-dedicated boxes, **NEVER** a shared
multi-tenant box*. That gate would have permanently blocked imagineering, the one box
`cutover.sh` exists for, and a machine-forced `docker restart livekit` there would bounce matrix,
outline and a dozen bots. The gate now requires renewal to have a **named owner**
(`CERT_RENEWAL_OWNER=timer|runbook`) — fail-closed with a declared opt-out, because fail-closed
with no way through just gets commented out by the first operator who hits it.

## Rig capability added

The rig previously **could not** complete a relay-only WebRTC call at all: its SFU advertised an
RFC1918 address, which the TURN relay correctly refuses as a peer. `provision.sh` now aliases a
non-private address (`203.0.113.5`, TEST-NET-3) and points `node_ip` at it, persisted through
reboot. The rig can now run the real-client relay proof end to end.

## migrate-to-passthrough.sh — rehearsed against enspyr's ACTUAL current state

The cutover path is the easy one to rehearse; the risky artifact is the one that runs on the
**live, working** island. So the rig was driven backwards into enspyr's exact present shape —
the pre-task-#8 `cutover.sh` and `haproxy-cert-sync.*` restored from git at `92d2036` and run
for real, producing `external_tls: true`, HAProxy terminating on `:443`, plaintext `:5349`
firewalled, cert-sync timer active — and then migrated forward.

| claim | result |
|---|---|
| Phase 0 reads the cert **from inside the container** | ✅ passed on a correct mount |
| Phase 1 `livekit.yaml` edit is minimal + scoped | ✅ **3-line** diff (`external_tls` out, `cert_file`/`key_file` in); nothing else touched |
| Phase 1 verifies by cert **subject**, not "a handshake happened" | ✅ |
| **no dark window on `:443`** | ✅ `haproxy` `NRestarts: 0`, `:443` bound continuously — a graceful reload, not a restart |
| Phase 3 removes cert-sync entirely | ✅ 0 unit files, 0 files left in `/etc/haproxy/certs`, PEM **shredded** |
| Phase 4 renewal-ownership gate | ✅ |
| no residue | ✅ both `.pre-passthrough` staging files consumed |
| B3 pinned `tls:*:443` | ✅ OK / exit 0 |
| real Chromium client, UDP blocked | ✅ `relay/tls`, exit 0 |

### The B3 gate BLOCKed first, and it was right to

The first post-migration gate run returned **`BLOCK` / exit 2**, not OK. Cause:
`SSLCertVerificationError` — Pebble mints a new issuance root on every container start, the
guest had rebooted during the INV-3 test, and the system trust store held a root that no longer
signed anything. A rig artifact, not a migration fault.

What matters is the behaviour: the probe **refused to certify** on the strength of the working
`udp:3478` endpoint alone. That is exactly finding F1's fix (`B3_REQUIRE_ENDPOINT`) doing its
job on a live run, unprompted — the same run under the pre-F1 probe would have returned OK.

### And one more instrument fail-open, in my own hands

An earlier attempt at this verification reported `B3_EXIT=0` while the probe had not run at all:
`/tmp` was wiped by the reboot, taking the venv with it, and `$?` after a pipeline reports the
status of `cut`, not of the python process. A dead instrument reporting success through a
pipeline that could only ever print zero.

Fixed twice over: the probe venv now lives in `/opt/probeenv` (survives a reboot), and exit
status is captured directly rather than through a pipe. **Four fail-opens in one session, every
one in a measuring device rather than in the thing being measured** — that ratio is a property
of this verification layer, not a run of bad luck.

---

# Cage-match rounds 1-2 (2026-08-14/15) — what the adversaries found in the passthrough build

Two rounds, four families. **Kelvin APPROVED both times.** Carnot and Tesla each returned
`REQUEST_CHANGES` twice, and between them found four defects that would have taken a live island
down or certified a broken one. A single adversary would have shipped this.

## Round 1 — 8 real findings

The two worst were the **same defect at opposite ends of the pair**, found by two different
families: *mutate first, discover the wrong universe second*.

| finding | who | proof |
|---|---|---|
| `cutover.sh` disabled HAProxy before asserting anything about the box | Carnot | **RED-proved on the rig** with the pre-fix script: `haproxy active→inactive`, `:443` **UNBOUND**, chat `curl 000` |
| `rollback.sh` freed `:443` before checking a stock Caddyfile existed to hand it back to | Tesla | the RUNBOOK's own "rollback anytime" line was an outage generator on a migrated box |
| Phase 3 verified with a **hardcoded** `curl https://chat.enspyr.co/` | Carnot + Tesla | on imagineering that leaves the box, hits enspyr, and **passes** — a check that succeeds by measuring a different machine |
| Phase 4's "idempotent from here" was false — staging files left, Phase 0 refuses | Carnot + Maxwell | the one failure path designed to be re-runnable was the only one that bricked the re-run |
| `restore_both` warned and returned on an unverified rollback | Kelvin | fail-apathetic: the caller's `die` buried the five-alarm |
| unescaped `TURN_DOMAIN` in a grep regex, ORed in front of `-checkhost` | Carnot | a weaker check before a stronger one can only weaken it |
| cert-sync removed from `/usr/local/sbin`; the old cutover installed to `/usr/local/bin` | Maxwell | **verified on live enspyr** — the script is still on disk there |
| root rendering to a predictable `/tmp` path | Maxwell | symlink clobber |

## Round 2 — 9 real findings, 1 rejected

**The headline: a check I added in round 1 was blind to the failure it was written for.**

Comparing the cert fingerprint on `:443` against LiveKit's on `:5349` was introduced as "the
positive proof that this is passthrough". It is not. `Caddyfile.mux` deliberately keeps a
`turn.` site block, and Caddy serves it **from the same cert store LiveKit mounts** — so a
mis-rendered SNI rule that dumps turn into `default_backend be_caddy` produces a byte-identical
cert. RED-proved by breaking the SNI rule for real:

```
cert valid for turn.enspyr.co through :443?   YES
fingerprints match?                           YES   <- the round-1 check PASSES on a broken mux
PASSTHROUGH-PATH check                        FAIL  <- the round-2 check catches it
after restore                                 PASS
```

The fix discriminates the **path**, not the certificate: Caddy answers `respond "turn" 200` to
an HTTPS GET; LiveKit's TURN socket cannot speak HTTP at all. A successful GET for the turn name
through `:443` therefore *proves* the misroute.

This is the verifier-shares-a-representation-with-the-verified class, and it is the fourth
distinct instrument failure in this workstream. The pattern is now unambiguous: **in this
subsystem, the measuring devices fail more often than the thing being measured.**

Other round-2 finds: `restore_both` verified recovery by an `:443` handshake that completes even
with LiveKit dead (HAProxy terminates `:443` under the old shape — measuring the front
oscillator says nothing about the pair); Phase 3 shredded the PEM *before* consuming the staging
files, so a kill in between left Phase 0 printing a "restore BOTH" recipe that reassembles an
unbootable config; Phase 4 checked `is-active` without `is-enabled`, so a `start`-without-`enable`
greened the gate and evaporated on reboot; `checks.sh` skipped its backend assertion entirely
when `:5349` had no owner (fail-open by omission); the resume detector used whole-file greps
while the mutation it guards is `turn:`-scoped; `TURN_DOMAIN` reached `sed s///` unvalidated as
root; and the rollback guard was keyed on unit-active state rather than port ownership.

**Rejected with proof:** Carnot's `$HA_CFG.new survives abort paths` — it is `rm`'d on both
(`migrate-to-passthrough.sh:186,190`). Carnot cited round-1 line numbers.

## Where this leaves the rig

Full cutover green, 7/7 invariants (including the new `PASSTHROUGH-PATH`), 18/18 fault
assertions, migration rehearsed against enspyr's real pre-migration shape, resume path proven
both directions. `refresh-ca` was added to `drive.sh` because Pebble's per-restart root rotation
cost two diagnosis detours in one session.

## Round 3 — the fix landed in 3 of 4 places

Kelvin APPROVE (third time), Carnot and Tesla `REQUEST_CHANGES` again. Four real findings, and
the sharpest was about round 2's own fix.

**Tesla:** the path discriminator went into `cutover.sh`, `checks.sh` and `faults.sh` — and NOT
into `migrate-to-passthrough.sh`, which is the script that runs on **enspyr**: the already-muxed
live box, no dark window, no second chance. It was still printing `MIGRATION COMPLETE` on the
strength of a cert check that round 2 had just proved cannot see a misroute. I fixed *instances*
rather than the *class*.

So the assertions now live in **one file** (`lib/turn-assert.sh`) sourced by both deploy scripts
and the rig harness: `turn_domain_valid`, `turn_tls_ok`, `turn_path_is_passthrough`. "We fixed 3
of 4" stops being a reachable state.

**RED-proved on the rig** by removing `use_backend be_turn` from the template and running the
real migration against the real old shape:

```
verifying a real TLS handshake for turn.enspyr.co THROUGH :443   <- SUCCEEDS (the cert lies)
restoring the previous haproxy.cfg and reloading
restoring livekit.yaml and restarting
restore verified: :443 answers AND :5349 is back to plaintext
ABORT: an HTTPS GET for turn.enspyr.co through :443 SUCCEEDED — answered by CADDY
```

One fault injection exercised three round-2/3 fixes at once: the Phase 2 path check caught it,
`restore_both` unwound **both** coupled artifacts, and the new dual restore verification
confirmed the pair back in phase rather than measuring only the front oscillator.

Also fixed: `-checkhost` verified the name but not expiry or chain while the messages said
"valid cert" (Carnot) — `turn_tls_ok` now checks name AND expiry, and warns on an unverifiable
chain rather than fail-closing on a private CA; `cutover.sh` lacked the DNS-label validation
`migrate` had (Carnot); and the staging-file guards ran *before* the shape detection, so the
window between a successful Phase 2 and Phase 3's cleanup made a re-run print a recipe that
reassembles the out-of-phase pair (Tesla) — the running shape now arbitrates, and stale stocks
on an already-passthrough box are discarded rather than offered as a trap.

Two more of my own, found by running rather than reading: `die()` printed *"nothing mutated past
this point"* on paths that had mutated and restored (now `die_restored`), and a fault assertion
grepped for a sentence I had reworded — a test asserting prose rather than a fact.

**Rig after round 3:** full cutover green (96 ms), 7/7 invariants, **18/18** faults, migration
RED/GREEN proved, resume-with-stale-stocks proved.
