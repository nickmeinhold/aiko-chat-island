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
| `rollback.sh` | restores Caddy to :443; safe to run standalone anytime |
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
honest scope. Harness: [`rehearsal/`](rehearsal/). **F1 (the B3 gate fails open on
`turns:443` being dead) should be fixed before the live cutover — it is the check that would
tell you the cutover failed.**

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
sudo OFF443_PROVEN=1 TURN_DOMAIN=turn.<domain> bash deploy/media/turn-443/cutover.sh
# on a SHARED box where cert-restart.timer must not be installed:
sudo OFF443_PROVEN=1 TURN_DOMAIN=turn.<domain> CERT_RENEWAL_OWNER=runbook bash .../cutover.sh
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
sudo TURN_DOMAIN=turn.<domain> bash deploy/media/turn-443/migrate-to-passthrough.sh
```

Phase 0 preconditions (incl. reading the cert **from inside the container**, so a mount that
looks right on the host but is wrong in the container fails here rather than at handshake time)
→ Phase 1 `livekit.yaml` back to its own TLS + restart, verified by cert **subject** →
Phase 2 render + install the passthrough config, `haproxy -c`, graceful reload, verify a real
handshake through :443 → Phase 3 shred the PEM and remove cert-sync → Phase 4 renewal ownership.
Each phase restores what it touched on failure. **No dark window on :443.**

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

> **Dependency:** `b3_relay_probe.py` lives on PR#129 (`feat/b3-behavioral-probe`), which is
> stacked on PR#128. Until both merge, `main` still carries the older `TURN_B3_PRIVATE_DENY_CMD`
> gate — so merge #128 → #129 before the cutover, or you are certifying production with tooling
> that only exists in a PR.

Also prove INV-1 from **off-box** (the on-box guard is v4+v6 host-INPUT, valid only because
LiveKit is host-networked — cutover asserts that): from your laptop,
`openssl s_client -connect <enspyr-public-ip>:5349` must **fail/refuse** (public plaintext :5349
is closed). A localhost probe can't prove external closure; this can.

**IPv6 (pick-a-universe, Kelvin):** the box has NO public IPv6 today, so cutover's `ip6tables`
rules are proactive future-proofing (harmless now). The off-box v6 probe is therefore
**conditional but MANDATORY-if-present**: if `ip -6 addr show scope global` on the box is ever
non-empty, the off-box `openssl -6 -connect [<v6>]:5349` closure proof becomes a **blocking**
sign-off step — a public v6 plaintext endpoint must never exist unverified.

## If the cutover dies mid-flight (recovery, not theory)

- **`cutover.sh` hard-killed between 2.3 and 2.4** (Caddy has released `:443`, HAProxy hasn't
  taken it): `:443` is UNBOUND and chat is down, and **nothing will fix it on its own** — the
  unit is enabled but not started. Recover with `sudo systemctl start haproxy` (or reboot: the
  persisted state is boot-correct, proven in the rehearsal). This is the one window with no
  watchdog; it is ~100 ms wide in a normal run.
- **A clean rollback does NOT mean a healthy system.** `rollback.sh` restores the `.stock`
  files, i.e. *whatever was there when cutover started* — if that was already broken, you get
  it back, broken. Rollback's hard-gate protects the security invariant (it refuses to reopen
  the `:5349` firewall unless TLS is genuinely being presented), not service health. Read its
  final lines: if it says it refused to reopen the firewall, TURN is down-but-closed and needs
  a hand.

## Known windows + limitations (named, not hidden)

- **Dual dark window during cutover (2.1→2.4):** once :5349 is firewalled (2.1) and before HAProxy
  binds :443 (2.4), BOTH TLS-TURN ingresses are down (public :5349 closed, :443 not yet TURNS).
  This is intentional blast, not a bug — the advertised `turns:443` is already dead pre-cutover, so
  no working relay is interrupted. The separate :443 *reload→bind* gap (chat/signaling) is the
  millisecond dark window `cutover.sh` measures and logs.
- **Client IP on the non-turn path is preserved via PROXY protocol** (HAProxy `send-proxy-v2` →
  Caddy `:8443` `proxy_protocol`), so the gateway's per-IP auth rate limiter still sees real IPs.
  **The TURN path (`be_livekit_plain`) does NOT send PROXY protocol** — LiveKit's embedded TURN
  isn't configured to trust it, so LiveKit sees the relay client as `127.0.0.1`. TURN *allocation*
  works regardless; any LiveKit-side per-source-IP logic on the relay is the accepted Shape-C tax
  (add `send-proxy` there only if LiveKit is later configured to expect it).

## Gate

**Code cage-match this config + the cutover/rollback scripts before the live cutover** (the
design temper does not cover the implementation). Round 1 (2026-08-12) returned unanimous
REQUEST_CHANGES on the prose-only version; this revision ships the scripts + fixes — re-run
the cage-match on it before deploying.
