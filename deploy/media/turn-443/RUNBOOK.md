# TURN-over-TLS on :443 — build + cutover runbook (enspyr, task #4)

Architecture: [`haproxy.cfg.tmpl`](haproxy.cfg.tmpl). **Shape: plain SNI passthrough** (task #8;
this replaced Shape C / `external_tls` on 2026-08-14). See
`docs/crucible/turn-tls-443-relay/DESIGN.md` for the design and its premise corrections.
**The cutover + rollback are SCRIPTS, not prose** — this runbook orchestrates them; it is not
the thing you hand-execute on the live front door.

Artifacts in this dir:

| File | Role |
|---|---|
| `haproxy.cfg.tmpl` | the :443 SNI mux, `@@TURN_DOMAIN@@`-templated (rendered to `/etc/haproxy/haproxy.cfg`) |
| `Caddyfile.mux` | Caddy moved to loopback:8443 + turn cert-issuance block (HTTP-01 forced) |
| `cutover.sh` | fail-closed, auto-rollback-on-red live cutover — for a box where **Caddy** still owns :443 |
| `rollback.sh` | restores Caddy to :443 — unwinds a **cutover**, not a migration (it refuses if there is no stock Caddyfile to hand :443 back to) |
| `migrate-to-passthrough.sh` | for a box **already running the mux** (enspyr): backend swap, no dark window |

## Which script do I run?

| Box state | Script | :443 dark window |
|---|---|---|
| Caddy owns :443, no mux (imagineering) | `cutover.sh` | ~95 ms, measured |
| HAProxy already owns :443 on the old `external_tls` shape (enspyr) | `migrate-to-passthrough.sh` | **none** — HAProxy holds :443 throughout; only its backend changes, via graceful reload |

## The 2 coupled artifacts

The cutover changes **two** pieces of state plus one firewall rule. It used to change four, in
a security-critical order, because `external_tls` put LiveKit on plaintext TURN and every other
step had to be sequenced around that window. There is no window now, so there is no sequence to
get wrong:

| # | Artifact | Cutover | Rollback |
|---|---|---|---|
| 1 | `/etc/caddy/Caddyfile` | → `Caddyfile.mux` (loopback:8443) | restore `.stock` (public :443) |
| 2 | HAProxy | render template, install, start on :443 | stop + disable (frees :443) |
| — | `iptables` :8443 | DROP non-loopback | remove DROP |
| — | **`livekit.yaml`** | **never touched** | **nothing to restore** |

`rollback.sh` still orders things so **:443 is never owned by two processes**. What it no
longer needs is the hard gate that refused to reopen the firewall until it could prove :5349
was speaking TLS — the single most dangerous step in the old runbook, and the one that would
have exposed plaintext TURN to the internet had it ever passed vacuously.

## Ports after cutover

| Port | Before | After |
|---|---|---|
| `:443` (public) | Caddy (all TLS) | **HAProxy** — SNI mux, terminates nothing |
| `:8443` | — | Caddy HTTPS (moved off :443); **firewalled to loopback** (only HAProxy reaches it) |
| `:5349` | LiveKit TURN/TLS (own cert) | LiveKit TURN/TLS (own cert) — **unchanged**; now also reachable via :443 |
| `:80` | Caddy (HTTP-01) | Caddy (HTTP-01) — **unchanged**, the renewal path |
| `:3478/udp`, relay 50000-60000 | LiveKit | unchanged |

## Cert renewal — who owns it (the time-bomb, relocated not removed)

Once HAProxy owns :443, a TLS-ALPN-01 renewal would hit HAProxy and fail silently → cert rots
→ TURNS dies ~89d out. And under passthrough **LiveKit holds the cert**, and pion/turn serves it
from memory with no hot-reload. Closed two ways:

1. **Deterministic HTTP-01.** `Caddyfile.mux` keeps a `turn.<domain>` block with
   `tls { issuer acme { disable_tlsalpn_challenge } }` — Caddy stays the ACME client and renews
   via HTTP-01 on :80 (untouched). Keeping the block also prevents "issuance dies when you
   unstub" (Tesla). LiveKit reads that same store directly via the `/certs` mount, so there is
   no copy to keep in sync and no private key crossing a uid boundary.
2. **A named owner for the restart.** Both deploy scripts **refuse to finish** without one:
   - `CERT_RENEWAL_OWNER=timer` (default) — requires an active `cert-restart.timer`. For
     island-dedicated BOOTSTRAP boxes (enspyr).
   - `CERT_RENEWAL_OWNER=runbook` — for a **shared multi-tenant box** (imagineering), where
     `cert-restart.service` must NOT be installed by contract: a machine-forced
     `docker restart livekit` there would bounce every other tenant. A human owns the restart;
     the cutover warns loudly if `served-cert-alarm.timer` is not running either, because an
     unowned renewal and an unobserved one fail identically, months later, with a green board.

**Propagation is not prompt, and that is deliberate.** `cert-restart.sh` is a *staleness* guard:
it restarts only once the **served** cert falls inside `ALARM_NOTAFTER_DAYS` (default 14). Caddy
renews at ~30 days remaining, so a renewed cert can sit on disk unserved for **~16 days**. The
served cert stays valid throughout, so there is no user impact — but the honest claim is *"a
renewal reaches clients before expiry"*, not *"promptly"*. `haproxy-cert-sync` propagated on
fingerprint change; this does not. Verified on the rig, both halves (no-thrash while fresh;
propagates once stale).

## Build + PROVE OFF THE LIVE :443 (do ALL of this before running cutover.sh)

**STATUS 2026-08-12: DONE.** A full disposable-VM rehearsal ran the real scripts against real
systemd/iptables/Caddy/HAProxy/LiveKit + a real ACME server: 4 reboot-at-checkpoint runs, 5
rollback runs, 3 fault injections, and a genuine TURN-over-TLS allocation through the mux.
See [`rehearsal/RESULTS.md`](rehearsal/RESULTS.md) for the evidence, the two findings, and the
honest scope. Harness: [`rehearsal/`](rehearsal/). F1 (the B3 gate fail-opening on a dead
`turns:443`) is **FIXED** — pin `B3_REQUIRE_ENDPOINT` and the probe BLOCKs instead of certifying
on the strength of the UDP endpoint alone. It did exactly that during the passthrough rehearsal.

`cutover.sh` refuses to run unless `OFF443_PROVEN=1` — you assert you have:

1. `haproxy -c -f <rendered haproxy.cfg>` → exit 0 (config check, no bind). Under passthrough
   this is checkable unconditionally — the config references no cert, so there is no
   "deferred until the PEM exists" state.
2. Proven a real TURNS allocation through the mux on the rehearsal rig, with `b3_relay_probe`
   and `B3_REQUIRE_ENDPOINT` pinned.
3. Negative tests: chat + livekit SNI passthrough still 200/WSS; unknown SNI answered
   identically to Caddy's own answer; `acme-tls/1` ALPN; malformed/slow ClientHello; non-TLS
   junk (rejected); a rejected handshake does not poison the acceptor.
4. Cert-renewal rehearsal: force a `turn.<domain>` renew and confirm **both** halves — no
   restart while the served cert is fresh (anti-thrash), and propagation once it reads stale.

## Cutover (the ONE irreversible step)

```bash
# CERT_RENEWAL_OWNER is REQUIRED — there is no default, because the only sensible default for
# this script's actual customer (imagineering, shared) is the one the contract forbids.
sudo OFF443_PROVEN=1 TURN_DOMAIN=turn.<domain> CERT_RENEWAL_OWNER=runbook \
  bash deploy/media/turn-443/cutover.sh          # SHARED box: a human owns the restart
sudo OFF443_PROVEN=1 TURN_DOMAIN=turn.<domain> CERT_RENEWAL_OWNER=timer \
  bash deploy/media/turn-443/cutover.sh          # island-dedicated box: timer must be enabled+active
```

It runs the sequenced state machine (Phase 0 preconditions incl. **asserting the passthrough
backend already serves TLS for the turn domain** → stage the backup → 2.1 firewall :8443 →
2.3 move Caddy → 2.4 start HAProxy → Phase 3 on-box verify → Phase 4 renewal ownership),
auto-rolling-back on any verify failure. It measures + logs the :443 dark window
(Caddy-release → HAProxy-bind); observed 90–95 ms across four rig runs.

`TURN_DOMAIN` is **required**, with no default. A default would have made it possible to run
imagineering's cutover with enspyr's domain baked into the SNI rule — a mux that silently
routes nothing.

## Migration (for a box already running the old `external_tls` mux)

```bash
sudo TURN_DOMAIN=turn.<domain> LIVEKIT_DIR=/home/<user>/apps/livekit \
  bash deploy/media/turn-443/migrate-to-passthrough.sh
```

Phase 0 preconditions — including reading the cert **from inside the container** (a mount that
looks right on the host but is wrong in the container fails here, not at handshake time) and
**asserting `cert-restart.timer` is enabled+active BEFORE anything is touched**. Then Phase 1
returns `livekit.yaml` to its own TLS and restarts, verified by cert **subject**; Phase 2 renders
and installs the passthrough config, `haproxy -c`, graceful reload, and verifies both the cert
**and the path** through `:443` (an HTTPS GET for the turn name must FAIL — Caddy would answer it,
LiveKit cannot); Phase 3 shreds the PEM and removes cert-sync. Each phase restores what it
touched, and an EXIT trap unwinds anything staged if the script dies somewhere unhandled.
**No dark window on :443.**

There is no resume path and no post-mutation gate: the renewal check that used to sit at the end
is a precondition, so a failure there costs nothing and leaves nothing half-done. Re-running on an
already-migrated box says "nothing to do" (it leaves `/etc/haproxy/.migrated-to-passthrough`).

## Acceptance gate (run from OFF-BOX after cutover.sh reports green)

Run the probe with the `turns:443` endpoint **pinned**, so "UNREACHABLE → ALLOCATED" is
enforced by the tool rather than promised by the operator:

```bash
B3_REQUIRE_ENDPOINT="tls:${TURN_DOMAIN}:443" B3_EXPECT_HOST="$TURN_DOMAIN" \
  python3 deploy/media/b3_relay_probe.py     # exit 0 = ALLOCATED; exit 2 = BLOCK
```

Without that pin the probe returns **OK/exit 0 even when `turns:443` is dead** — it treats an
un-allocatable endpoint as "not a relay vector, surfaced" and certifies on the UDP endpoint
alone. That is correct for its security question and fail-open for this one; the task #6
rehearsal caught it empirically (`rehearsal/RESULTS.md` F1). **Pin it, or the acceptance gate
will green-light exactly the failure this cutover exists to fix.**

Expect: `turns:443` ALLOCATED, UDP relay still allocates, all RFC1918/link-local/loopback/CGNAT
sentinels still 403, chat + signaling green.
**If it fails: `sudo bash deploy/media/turn-443/rollback.sh`.**

**Rollback caveat (Tesla):** `rollback.sh` unwinds a **cutover**, not a **migration**. It is
safe to run standalone on a box `cutover.sh` cut over. On a box that ran
`migrate-to-passthrough.sh`, the stock Caddyfile was consumed on success, so there is nothing
to hand `:443` back to — the script now REFUSES up front rather than freeing `:443` and then
discovering it (which is what the old ordering did: an outage produced by the documented
recovery command).

**There is no longer an off-box `:5349`-must-refuse proof, because there is no plaintext.**
The old acceptance step ("`openssl s_client -connect <public-ip>:5349` must fail/refuse")
belonged to `external_tls`, where `:5349` carried plaintext TURN and INV-1 existed to keep it
off the internet. Under passthrough `:5349` is an ordinary TURNS socket serving the same
service `:443` fronts. **An operator running that old check on a healthy passthrough box will
see `:5349` speaking TLS, conclude INV-1 has failed, and fire `rollback.sh` at a working mux.**
That is the failure mode of a stale runbook: prose that contradicts the scripts is not
documentation, it is an untested control plane.

What to check instead: the two acceptance gates below (B3 pinned to `tls:*:443`, and the
real-client relay proof). Note that on a box migrated from the old shape the `:5349` DROP may
still be present from the original cutover — harmless (`:443` fronts the same service), but it
means `:5349` may be closed off-box. Neither state is a fault.

**IPv6:** the boxes have NO public IPv6 today, so `cutover.sh`'s `ip6tables` rule for `:8443`
is proactive future-proofing. If `ip -6 addr show scope global` ever becomes non-empty, the
off-box v6 closure proof for **`:8443`** (Caddy's HTTPS, which must not be publicly reachable —
INV-8) becomes a **blocking** sign-off step.

## If the cutover dies mid-flight (recovery, not theory)

- **`cutover.sh` hard-killed between 2.3 and 2.4** (Caddy has released `:443`, HAProxy hasn't
  taken it): `:443` is UNBOUND and chat is down, and **nothing will fix it on its own** — the
  unit is enabled but not started. Recover with `sudo systemctl start haproxy` (or reboot: the
  persisted state is boot-correct, proven in the rehearsal). This is the one window with no
  watchdog; it is ~100 ms wide in a normal run.
- **A clean rollback does NOT mean a healthy system.** `rollback.sh` restores the `.stock`
  file, i.e. *whatever was there when cutover started* — if that was already broken, you get it
  back, broken. It no longer carries the old `:5349` hard-gate (there is no plaintext to gate),
  so read its final lines for what it actually did and verify service health yourself.
- **`migrate-to-passthrough.sh` hard-killed between Phase 1 and Phase 2** (LiveKit is on its
  own TLS, HAProxy still forwards plaintext): `turns:443` is DOWN and neither half looks wrong
  alone. Only `livekit.yaml.pre-passthrough` will exist — HAProxy was never touched — so unwind
  LiveKit alone: `mv -f <livekit.yaml.pre-passthrough> <livekit.yaml>` and restart the
  container. Re-running the script prints exactly this instruction.

## Known windows + limitations (named, not hidden)

- **The `:443` dark window during cutover** (Caddy releases → HAProxy binds) is the only
  interruption, measured and logged by `cutover.sh`; 90-95 ms across four rig runs. The old
  "dual dark window" (both TLS-TURN ingresses down between the firewall step and the bind) is
  **gone** — it existed only because 2.1 firewalled a `:5349` that 2.2 was about to make
  plaintext. `migrate-to-passthrough.sh` has no `:443` window at all.

- **Client IP on the non-turn path is preserved via PROXY protocol** (HAProxy `send-proxy-v2` →
  Caddy `:8443` `proxy_protocol`), so the gateway's per-IP auth rate limiter still sees real IPs.
  **The TURN path (`be_turn`) does NOT send PROXY protocol** — LiveKit's embedded TURN
  isn't configured to trust it, so LiveKit sees the relay client as `127.0.0.1`. TURN *allocation*
  works regardless; any LiveKit-side per-source-IP logic on the relay is the accepted tax
  (add `send-proxy` there only if LiveKit is later configured to expect it).

## Gate

**Code cage-match this config + the cutover/rollback scripts before the live cutover** (the
design temper does not cover the implementation). Round 1 (2026-08-12) returned unanimous
REQUEST_CHANGES on the prose-only version; this revision ships the scripts + fixes — re-run
the cage-match on it before deploying.
