# Shape B — TURN-over-TLS on :443 via a second IP (imagineering)

**Status: APPLIED AND VERIFIED LIVE on imagineering 2026-08-15.**

```
selected: turns:turn.imagineering.cc:443?transport=tcp @137.23.18.134
relayProtocol tls · all_relay true · 11427 B sent / 1927 B received
```

Real Chromium client, `iceTransportPolicy:relay`, browser UDP dropped. The UDP-open control
correctly selected `udp`. All 31 Caddy tenant sites verified serving afterwards, and all six SFU
consumers healthy with zero LiveKit errors. Previously rig-proven on `turnrig`
(claude-tasks#3051).

Live values: **IP1** `10.0.0.21` (Caddy) · **IP2** `10.0.0.22` · **PUB2** `137.23.18.134`
(reserved, `turn-imagineering-shapeb`).

This is the **alternative to `../turn-443/`** (the HAProxy SNI mux, live on enspyr). Read
[Which shape for which box](#which-shape-for-which-box) before picking. Shape B is *not* a
general improvement on the mux — it is the right shape for a box whose `:443` is shared with
tenants that are not ours.

---

## Why this exists

LiveKit **always advertises `turns:<turn.domain>:443`**, regardless of `turn.tls_port`
(hardcoded in `roommanager.go`; the vendor's own config-sample says `tls_port` "must be set to
443" when not behind a load balancer). Measured on both islands 2026-08-15:

```
imagineering: ["turn:149.118.69.221:3478?transport=udp", "turns:turn.imagineering.cc:443?transport=tcp"]
enspyr:       ["turn:158.179.17.233:3478?transport=udp",  "turns:turn.enspyr.co:443?transport=tcp"]
```

imagineering listens for TURN/TLS on `:5349`, which **no client is ever told about**, while
`:443` is Caddy (HTTP). So its advertised relay is a dead end. Because the app forces
`iceTransportPolicy: .relay` for peer-IP privacy, any user on a UDP-blocking network cannot
complete a call there at all. Confirmed with a real client: enspyr OK, imagineering
`could not establish pc connection`.

Something must own `:443` for the TURN domain. The mux does it by putting HAProxy in front of
everything; Shape B does it by giving LiveKit **its own IP**, so the shared front door is never
touched.

## Which shape for which box

| Box `:443` | Shape | Why |
|---|---|---|
| Dedicated to the island (enspyr) | **Mux** (`../turn-443/`) | one front door, one tenant; already live |
| **Shared with unrelated tenants (imagineering)** | **Shape B (this doc)** | Caddy's config is never replaced, so ~30 third-party sites are never at risk |

**Do not run `../turn-443/cutover.sh` on imagineering.** It wholesale-replaces the target
Caddyfile with a 3-site enspyr-hardcoded `Caddyfile.mux`; imagineering serves **31** sites
including `n8n.callonclare.com.au` and the `xdeca.com` set. See claude-tasks#2962 / #3120.

---

## Blast radius — read this before starting

Shape B leaves the 31-site Caddy front door **untouched**. It is not, however, zero-risk:

1. **The SFU is shared.** imagineering's LiveKit serves six consumers besides the island —
   `dreamfinder`, `dreamfinder-avatar`, `lyra-avatar`, `symposium`, `tech-world-bots`,
   `aiko-chat-gateway`. Step 4 changes `rtc.node_ip`, which moves the advertised media path
   **for all of them**, and step 6 restarts LiveKit, which **bounces all of them**. Pick a
   window, and verify them in step 8 — not just the island.
2. **Caddy gets pinned to specific addresses.** It currently binds `*:443`. Anything reaching
   it on an address not in `default_bind` (a container talking to the host, a loopback health
   check) stops working. Enumerate before, verify after.
3. **DNS moves.** `turn.imagineering.cc` repoints to the new public IP. The existing certificate
   stays valid across the move (certs bind names, not addresses), but **renewal** breaks unless
   step 3's `http://` block is in place.

Nothing here is irreversible: every step is a config edit or an address add, and rollback is
"put the two files back and restart" (see [Rollback](#rollback)).

---

## Preconditions

- [ ] A **reserved** (not ephemeral) public IP is available in the tenancy. Free — verified
      2026-08-14: `reserved-public-ip-count` limit 50, 1 in use, no public-IP SKU in 30 days of
      billing. **Check imagineering's existing public IP is itself reserved** before adding a
      sibling — claude-tasks#3057 records that ant-1/ant-2 run on *ephemeral* IPs that change on
      stop/start, and an ephemeral IP under a TURN domain is a time bomb.
- [ ] A maintenance window agreed with whoever owns the six SFU consumers above.
- [ ] The real-client proof runs green against **enspyr** first, as a known-positive control for
      the instrument (`deploy/media/webrtc_relay_proof.py`). If the control is red, fix the
      instrument before touching imagineering.

Box facts (measured 2026-08-15):

| | value |
|---|---|
| VNIC | the instance's primary VNIC, region `ap-sydney-1` — read the OCID off the box with `curl -s -H 'Authorization: Bearer Oracle' http://169.254.169.254/opc/v2/vnics/` (not recorded here: this repo is public) |
| primary private / public | `10.0.0.21` / `149.118.69.221` |
| subnet | `10.0.0.0/24` |
| Caddy | container `caddy:2-alpine`, host-networked, config `/home/nick/apps/caddy/Caddyfile` → `/etc/caddy/Caddyfile` |
| LiveKit | container `livekit/livekit-server:v1.13.5`, host-networked, config `/home/nick/apps/livekit/livekit.yaml` |
| cert store | Caddy volume, mounted into LiveKit at `/certs` |
| no `oci` CLI on the box | drive OCI from the console or a workstation |

Throughout: **IP1** = `10.0.0.21` (primary private, keeps Caddy), **IP2** = the new secondary
private IP (say `10.0.0.22`, LiveKit's), **PUB2** = the reserved public IP mapped to IP2.

---

## Steps

Run these in order and **verify each before the next** — each one preconditions the one after.

### 1. Add the secondary private IP + reserved public IP (OCI control plane)

Console → Compute → Instance → *Attached VNICs* → the VNIC above → **IPv4 Addresses** →
*Assign Private IP Address*: `10.0.0.22`. On that new private IP, *Assign Public IP* →
**Reserved** → create/attach → note **PUB2**.

Verify: the VNIC lists two private IPs, and `10.0.0.22` shows PUB2.

### 2. Configure IP2 in the OS

OCI does 1:1 NAT — the OS only ever sees private IPs. The secondary IP must be added to the
interface or nothing binds it.

```bash
sudo ip addr add 10.0.0.22/24 dev enp0s3        # confirm the real iface name first: ip -4 addr
ip -4 addr show | grep inet                     # expect BOTH 10.0.0.21 and 10.0.0.22
```

**Make it survive reboot.** On imagineering this is `/etc/netplan/60-shapeb-secondary-ip.yaml`
(mode 600), validated with `netplan generate` and **deliberately not `netplan apply`** — apply can
bounce a live interface, and `ip addr add` already did the work for this boot.

> **OUTSTANDING on imagineering:** the reboot has not been done, so persistence is *validated but
> not proven*. Until a reboot confirms it, treat `10.0.0.22` as this-boot-only — if the box
> restarts and the address does not come back, TURN dies with it. Verify at the next planned
> reboot.

The local firewall needs no new rule — every INPUT rule is destination-agnostic, so the new
address inherits them. Confirm with `sudo iptables -S INPUT`.

> **Instrument warning:** `iptables -L | grep udp` does **not** show `multiport` rules — they
> render as protocol `17` with no literal "udp". Grepping that way made the RTC range
> (`7882:7892`) and the TURN relay range (`30000:40000`) look closed on imagineering when both
> are open. Read `iptables -S`, not a grep of `-L`.

### 3. Pin Caddy to IP1 and give ACME a `:80` on IP2

imagineering's Caddyfile has **no global block** — it starts straight at `outline.imagineering.cc`.
Add one at the very top:

```caddyfile
{
	default_bind 10.0.0.21 127.0.0.1
}
```

`127.0.0.1` is included deliberately: pinning drops every address not listed, and local callers
reaching Caddy on loopback would otherwise break.

Then change the existing `turn.imagineering.cc` block and add its ACME spine:

```caddyfile
# Cert issuance ONLY — LiveKit serves this cert itself on PUB2:443, so Caddy must never
# answer :443 for this name. TLS-ALPN-01 would arrive at PUB2:443 (LiveKit, not Caddy) and
# could never be solved, so HTTP-01 is forced.
turn.imagineering.cc {
	tls {
		issuer acme {
			disable_tlsalpn_challenge
		}
	}
	respond "turn" 200
}

# The ACME spine. `bind` takes HOSTS ONLY — the port comes from the site address — so an
# http:// block yields :80 on IP2 and never touches IP2:443. Without this, HTTP-01 for
# turn.imagineering.cc dies once DNS points at PUB2:
#   "dial tcp <PUB2>:80: connect: connection refused"   (measured on the rig)
http://turn.imagineering.cc {
	bind 10.0.0.22
	respond "acme-spine" 200
}
```

> **Why not just rely on the auto HTTP server?** Caddy's docs say the automatic
> HTTP-to-HTTPS redirect server does not inherit `default_bind`. Measured on the rig, `:80`
> bound **IP1 only** anyway. Trust the measurement: declare the block.

Validate **inside the container** (there is no `caddy` binary on the host):

```bash
docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

### 4. Move LiveKit's TURN to IP2:443

Edit `/home/nick/apps/livekit/livekit.yaml`:

```yaml
rtc:
  use_external_ip: false        # explicit node_ip must win; do not let detection override it
  node_ip: <PUB2>               # was 149.118.69.221 — see the trap below
turn:
  tls_port: 443                 # was 5349
  bind_addresses:
    - 10.0.0.22                 # IP2 — binds BOTH the TLS and UDP TURN listeners
```

> **The trap this exists to avoid.** `bind_addresses` moves the TURN **UDP** listener too, but
> the advertised UDP relay URL is built from `rtc.node_ip`, not from `bind_addresses`. Leave
> `node_ip` on the primary and clients are handed `turn:<primary>:3478` where **nothing is
> listening** — the fast UDP relay path dies silently for every user and all relay traffic
> degrades to TCP, with no error anywhere. Measured on the rig; fixed by aligning `node_ip`.
> `bind_addresses` is **undocumented** (absent from `config-sample.yaml`, present in
> `pkg/config/config.go`) — re-check it on any LiveKit upgrade.

Leave `cert_file`/`key_file` pointing at the Caddy-managed store mounted at `/certs`. Do **not**
set `external_tls` — nothing terminates TLS in front of LiveKit in this shape, which is the
entire point: no plaintext window, no cert-sync, no HAProxy.

Do **not** copy `allow_restricted_peer_cidrs` from the rig. That line exists only because the
rig uses `203.0.113.0/24` (TEST-NET-3), which LiveKit denies as a restricted peer CIDR. On a box
with globally-routable IPs it is unnecessary, and it would weaken the SSRF guard from #6.

### 5. Point DNS at PUB2

`turn.imagineering.cc` A record → **PUB2** (Namecheap, API-drivable). Lower the TTL first if it
is long. Everything else (`livekit.`, `chat.`, the other 30 sites) stays on the primary.

Verify **against the authoritative server**, not your resolver:
`dig +short turn.imagineering.cc @1.1.1.1` returns PUB2.

> **imagineering specifics.** There was no A record for `turn` — it resolved via a
> `*.imagineering.cc` wildcard. A *specific* record beats the wildcard, so adding
> `turn A <PUB2>` (DNS-only, **never** proxied — Cloudflare proxying would break TURN outright)
> moves only that name and leaves the other 30 subdomains on the primary.

> **The propagation hazard — and why `openssl` will lie to you here.** Caddy on IP1 keeps a
> `turn.<domain>` block (it must: Caddy is the ACME client). So during propagation, a client
> holding a stale answer connects to **Caddy** on the primary IP and gets a **valid certificate
> for the turn domain** — `Verify return code: 0 (ok)` — with no TURN behind it. That is a green
> TLS handshake onto a dead path, and it cost a false "Shape B is broken" reading during this
> very cutover: the test vantage had a stale resolver entry. **Always confirm which IP you
> actually reached** (`--resolve`, `getent hosts`, or `openssl -connect <PUB2>:443`) before
> concluding anything about the server. The mux shape's gate checks the mirror image of this
> (`cutover.sh:303`).

### 6. Apply

```bash
docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile   # no downtime
docker restart livekit                                                            # BOUNCES ALL SIX CONSUMERS
```

### 7. Verify — the acceptance gate

Run `./verify-shapeb.sh` (see below). It fails closed and checks, in order: both `:443`
listeners coexist, ACME is reachable on IP2:80, the advertised ICE servers match the actual
listeners, and **a real client relays over `turns:443` with UDP blocked** — with the UDP-open
control, so an instrument fault cannot read as a pass.

### 8. Verify the neighbours

The gate above proves the island. It says nothing about the other five SFU consumers. Exercise
each one that matters (`dreamfinder`, `lyra-avatar`, `symposium`, `tech-world-bots`) and confirm
media still flows. A green island with a dead Dreamfinder is not a successful cutover.

---

## Rollback

There is no state machine here because there is no dark window and no plaintext window — Caddy
never stops owning the front door, and LiveKit's move is a config edit.

1. `livekit.yaml`: restore `tls_port: 5349`, drop `bind_addresses`, restore `node_ip`.
2. `Caddyfile`: drop the global block and the `http://turn...` block; restore the plain
   `turn.imagineering.cc` block.
3. `docker exec caddy caddy reload ...` && `docker restart livekit`.
4. DNS: `turn.imagineering.cc` → back to the primary public IP.
5. Optionally `sudo ip addr del 10.0.0.22/24 dev <iface>` and release PUB2.

Rolling back returns the box to *today's* state — which is a **dead TURN relay**, not a working
one. Rollback is a safety net, not a destination; if you land here, imagineering still has the
outage this runbook exists to close.

## Cert renewal ownership

Unchanged from the mux shape and still a **shared-box contract**: `cert-restart.service` must
NOT be installed on imagineering, because a machine-forced `docker restart livekit` would bounce
every other tenant. A human owns the restart (`CERT_RENEWAL_OWNER=runbook`). LiveKit holds the
cert and pion/turn serves it from memory with no hot-reload, so a renewed cert on disk is not a
renewed cert in flight until LiveKit restarts. `served-cert-alarm.timer` is what makes that
visible; an unowned renewal and an unobserved one fail identically, months later, on a green
board.
