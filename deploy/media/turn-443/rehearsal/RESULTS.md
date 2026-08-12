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
