# TURN-on-443 mux — the invariant lattice

> **Read this as an instrument, not a trophy.** The previous version of this file listed **ten**
> invariants for one relay port, and I wrote it as evidence of rigor. It was evidence of
> something else: an invariant lattice getting long is a reading that the *shape* is too
> complex, not that the review was thorough. Nick's question — "can this be drastically
> simplified?" — was answered by this file the whole time, and I read it as a scoreboard.
>
> Under the passthrough shape (task #8) the count is **seven** (I wrote "six" first and the
> table said otherwise — in a file about honest measurement, count the rows). Three invariants
> are deleted outright, and the deletions are not
> simplifications but removals of the thing that made each one necessary. (INV-7 grew a
> second clause in the 2026-08-15 cage-match — a routing invariant whose only test was a cert
> check was not actually being tested.)

The surface used to be four coupled state machines transitioning together on a live shared
`:443`. It is now **two** — `Caddyfile` (C) and `HAProxy` (H) — plus one firewall rule.
`livekit.yaml` (L) is no longer part of the cutover at all, and `iptables` (F) went from a
security-ordered participant to a single tidy DROP.

## What survives

| # | Invariant | Enforced where | Owning writer | Proving test |
|---|---|---|---|---|
| **INV-2** | `:443` is owned by exactly one process; the dark window is bounded + measured | H/C: reload-Caddy → wait-for-release → bind HAProxy; `port_listening` poll | `cutover.sh` 2.3/2.4 | Measure the window; assert never 2 owners and never a lasting 0-owner state. **Not applicable to `migrate-to-passthrough.sh`** — HAProxy holds `:443` throughout, so there is no window to bound. |
| **INV-3** | Every intermediate state is **boot-correct** (a reboot mid-cutover lands somewhere safe) | H: unit `disable`d in Phase 0, `enable`d at 2.3 *before* the live Caddy reload; F: persisted DROP | `cutover.sh` | **Reboot at each checkpoint** → assert (a) exactly one `:443` owner, (b) chat still served. Cheaper than it was: with no plaintext flip there are fewer intermediate states to be wrong in. |
| **INV-4′** | The turn cert stays renewable, and a renewal reaches what clients see **before the served cert expires** | C: `turn.` site block with `disable_tlsalpn_challenge` (HTTP-01 on `:80`); L: reads Caddy's store directly via the `/certs` mount | Caddy (sole ACME client) + `cert-restart.sh` | Force a renewal → assert (a) no restart while the served cert is still fresh, (b) once it reads stale, the served cert advances. **This is the invariant that got harder**, not easier — see the propagation-lag note below. `cutover.sh` Phase 4 and `migrate-to-passthrough.sh` Phase 4 both **refuse to finish** unless renewal has a named owner. |
| **INV-5** | Real client IP is preserved on the non-TURN path (the gateway's per-IP rate limiter) | H: `send-proxy-v2` + `check-send-proxy`; C: `:8443` listener reads PROXY from 127.0.0.1 | `haproxy.cfg.tmpl` `be_caddy` + `Caddyfile.mux` | Hit chat through the mux from a distinct source IP; assert the gateway sees **that** IP, not 127.0.0.1. |
| **INV-6′** | Rollback restores both artifacts, idempotently | ordering in `rollback.sh` (HAProxy off → Caddy back on `:443` → reopen `:8443`) | `rollback.sh`; `.stock` staged + `cmp`-verified by `cutover.sh` Phase 1 | Roll back from each checkpoint; `cmp` the Caddyfile against a pre-cutover snapshot. Run it **twice** (idempotency). Run it with the `.stock` missing. |
| **INV-7** | SNI routing is correct, exposes nothing extra, **and the turn path really is passthrough** | H: `fe443` accepts only a real ClientHello, rejects the rest; `turn.` SNI → `be_turn` (raw); everything else → raw passthrough to Caddy | `haproxy.cfg.tmpl` `fe443` | Matrix: chat SNI → chat cert + response; unknown SNI → byte-identical to Caddy's own answer; **no** SNI; `acme-tls/1` ALPN; non-TLS junk (rejected); dribbled/slow hello (dropped at 5s). **Plus the path test, which the cert cannot give you**: `Caddyfile.mux` keeps a `turn.` block served from the same store LiveKit mounts, so a misroute into `be_caddy` yields a **byte-identical** cert — checkhost passes and fingerprints match on a broken mux (RED-proved 2026-08-15). So assert an HTTPS GET for the turn name through `:443` **fails**: Caddy would answer it, LiveKit's TURN socket cannot. |
| **INV-8** | Caddy's `:8443` is not publicly reachable (it is bound on all interfaces **on purpose**, so `:80` HTTP-01 keeps working — the r3 P0) | F: v4+v6 DROP on 8443 | `cutover.sh` 2.1 | Off-box connect to `:8443` refused; loopback connect succeeds. |

## What was DELETED, and why it is a deletion rather than a simplification

| was | why it existed | why it is gone |
|---|---|---|
| **INV-1** — plaintext TURN never publicly reachable | `external_tls: true` made LiveKit speak plaintext on `:5349` | Nothing speaks plaintext. There is no window to guard, so there is no guard to get wrong. |
| **INV-9** — no false green on the plaintext flip | a dead socket and a plaintext socket both fail a TLS handshake, so "no cert" was not proof of plaintext | There is no flip. The *positive* assertion (LiveKit really is serving a valid cert for the turn domain) survives, and moved to Phase 0 where aborting costs nothing. |
| **INV-10** — relay-deny survives the flip | the `awk` edit to `livekit.yaml` could have clobbered `deny_peer_cidrs` | `livekit.yaml` is never edited. The relay-deny property is still **verified** (b3 probe through the mux) but it is no longer an invariant this cutover can violate. |
| the ordering constraint | firewall **before** the flip; rollback reopens only **after** TLS is restored, behind a hard gate | Both ends of that constraint were the plaintext window. Steps are now ordered for tidiness, not safety. |
| `haproxy-cert-sync.{sh,service,timer}` + the PEM | HAProxy served a *copy* of Caddy's cert and needed it kept fresh | HAProxy holds no cert. The private key no longer crosses a uid boundary, and the `.needs-reload` sentinel — **the round-4 P0** — has nothing to sequence. |

**What the phase assertions do and do not prove.** `cutover.sh` Phase 0/3 and
`migrate-to-passthrough.sh` Phase 1/2 assert **TLS identity** (a handshake completes; the cert
verifies for the turn domain) and, since 2026-08-15, the **path** (the turn SNI is not being
answered by Caddy). They do **not** prove the TURN protocol stack behind the socket works — a
listener with a valid cert and a broken TURN implementation passes all of them. That is the
off-box B3 probe's job, with `B3_REQUIRE_ENDPOINT` pinned, and the real-client relay proof after
it. Written down because a phase that is read as proving more than it measures is how a green
board ends up over a dead media plane.

**Three limits the cage-match named, kept as limits rather than papered over.**

1. **The path probe discriminates Caddy-vs-not, not terminator-vs-passthrough** (Tesla). Both an
   HAProxy TLS terminator and LiveKit complete a handshake and then refuse HTTP, so the probe
   cannot tell them apart. It catches the misroute that actually exists in this shape (turn SNI
   falling into `be_caddy`); a reintroduced terminator is caught one layer up, by the config
   assertion that `ssl crt` appears nowhere in the rendered file. Two checks, one gap each,
   overlapping — worth stating so nobody reads the probe as proving more than it does.

2. **`migrate-to-passthrough.sh` has a window that is NOT boot-correct** (Tesla). Between Phase 1
   (LiveKit takes back its TLS) and Phase 2 (HAProxy stops forwarding plaintext), the pair is out
   of phase — and because LiveKit is `restart: unless-stopped` and HAProxy is unit-enabled, that
   state **survives a reboot**. INV-3 was only ever scoped to `cutover.sh`. This is inherent:
   two coupled processes cannot flip in the same instant. It is mitigated, not eliminated — the
   window is a container restart wide, an EXIT trap unwinds unhandled failures inside it, and
   Phase 0 detects and prints the exact recovery if a run died there. Named as a gap because it
   is one.

3. **`:5349` public is not the same surface as `:443`** (Tesla). "Same service" is true of the
   protocol and false of the door: `fe443` rejects a non-ClientHello within 5s and counts against
   `maxconn`, while LiveKit's raw TURNS socket has neither bouncer. Leaving `:5349` open is
   harmless as *"no plaintext is exposed"* and is not the same claim as *"no additional exposure"*.
   Operators who want the smaller surface should firewall it; nothing here depends on either choice.

**Honest accounting of what this cost.** One invariant got *harder*: INV-4′. Under
`external_tls` the terminating proxy could hot-reload a renewed cert with no media bounce; now
LiveKit must restart. That is the trade the whole shape turns on, and it is a good one — a
scheduled restart roughly four times a year, against a permanent plaintext window plus the six
mechanisms that existed to contain it. It is guarded by a hard gate in both deployment paths
rather than a comment, precisely because it is the one thing that got worse.

**And there is a second-order cost, found on the rig rather than reasoned about:** propagation
is no longer prompt. `haproxy-cert-sync` reloaded on *fingerprint change*, so a renewal reached
clients within the hour. `cert-restart.sh` is a **staleness** guard — it restarts only once the
*served* cert falls inside `ALARM_NOTAFTER_DAYS` (default 14). Caddy renews at roughly 30 days
remaining, so a renewed cert can sit on disk, unserved, for about **16 days**.

That is safe (the served cert is valid throughout) and it is deliberate: restarting a
multi-tenant media server on every fingerprint change is the thrash the two-clock guard exists
to prevent. But it means the honest statement of INV-4′ is *"a renewal reaches clients before
the served cert expires"*, **not** *"promptly"* — and the difference is worth writing down,
because the fault test that caught it was asserting the prompt version and failing correctly.

The load-bearing consequence: if `ALARM_NOTAFTER_DAYS` were ever raised above Caddy's renewal
margin, or the timer stopped running, the restart would never fire before expiry. The daily
timer and that threshold are now the only things standing between a renewal and a dead TURN
endpoint — which is exactly why Phase 4 refuses to finish without a named owner for renewal.

## Named, accepted gaps (not hidden)

- **The TURN path does not carry PROXY protocol** — LiveKit's embedded TURN isn't configured to
  expect it, so it sees the relay client as `127.0.0.1`. Allocation is unaffected; any
  LiveKit-side per-source-IP logic on the relay is blind. A security tradeoff accepted **by
  name**. Unchanged by the shape change.
- **IPv6 is proactive**: the boxes have no global v6 today, so the `ip6tables` rule guards a
  universe that does not yet exist. If `ip -6 addr show scope global` ever becomes non-empty,
  the off-box v6 closure proof for `:8443` becomes a **blocking** sign-off step.
- **`:5349` is left publicly reachable.** Under the old shape it *had* to be firewalled because
  it was plaintext. It is now an ordinary TURNS socket serving the same service `:443` fronts,
  so closing it is a surface-reduction preference, not an invariant. Stated so that its absence
  from `FW_PORTS` reads as a decision rather than an oversight.
