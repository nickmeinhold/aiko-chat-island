# HEAT / Research — TURN-over-TLS on :443 via caddy-l4 SNI mux

Research phase of the `/crucible` forge for making LiveKit's embedded TURN reachable
over TLS on :443 (so clients behind UDP-blocking / hostile firewalls can relay), on a
box where Caddy already owns :443.

**Target box:** enspyr (`chat.enspyr.co`, `ssh nick-mel`), Caddy **v2.11.4 stock** run as a
systemd binary, LiveKit **v1.13.5** with `udp_port 3478, tls_port 5349, relay_range 50000-60000`.

**Sources pinned to real code, cloned + grepped locally (not summarised from memory):**
- LiveKit server source `github.com/livekit/livekit@master` — `pkg/service/roommanager.go`, `pkg/service/turn.go`, `config-sample.yaml`
- caddy-l4 source `github.com/mholt/caddy-l4` @ **tag v0.1.2** (2026-07-15, commit 42db569)
- Caddy docs (automatic-https, ACME challenges JSON schema)

---

## HEADLINE (read this first)

Two findings dominate everything below:

1. **The "simpler alternative" (change LiveKit's advertised TURN port) DOES NOT EXIST.**
   The advertised port is **hardcoded to `:443`** in LiveKit source — there is no config
   knob. `turn.tls_port` only sets the *listen* port. So LiveKit *always* advertises
   `turns:<domain>:443?transport=tcp` and *always* listens on `tls_port` (5349). That
   permanent mismatch is exactly the bug we see, and it can only be closed by putting a
   TURNS responder on **:443** — which is what the layer4 mux does. The simpler
   alternative does **not** kill the design; it is impossible.

2. **The proposed mechanism has a strictly better variant than "move Caddy to :8443".**
   caddy-l4's documented idiom (`combining_apps.md`) keeps Caddy **natively on :443** and
   wraps its listener with a layer4 `listener_wrappers` block that peels off *only* the
   TURN SNI for passthrough; every other SNI (incl. ACME TLS-ALPN handshakes for
   `chat`/`livekit`) falls through to Caddy's own TLS terminator untouched. This removes
   the ACME-renewal risk that the "internal :8443" framing introduces. Recommend this
   variant.

---

## Q1 — caddy-l4 `tls` matcher: SNI passthrough without terminating TLS

**VERDICT: CONFIRMED (high confidence).** The `tls` layer4 *matcher* peeks the ClientHello
(SNI, ALPN) by parsing the raw handshake bytes and does **not** decrypt or terminate. A
`proxy` handler then forwards the byte stream — including the original ClientHello — so the
client's TLS session is established **end-to-end with the downstream** (LiveKit :5349).

Evidence — `modules/l4tls/matcher.go`, `MatchTLS.Match()` reads the record straight off the
buffered connection and parses the handshake header itself; it never completes a handshake:

```go
// modules/l4tls/matcher.go  (module ID: "layer4.matchers.tls")
rawHello := make([]byte, length)
_, err = io.ReadFull(cx, rawHello)              // cx = layer4.Connection (records + replays bytes)
...
if len(rawHello) >= 4 && rawHello[0] == 1 {     // 1 == ClientHello handshake type
    handshakeLen := int(uint32(rawHello[1])<<16 | uint32(rawHello[2])<<8 | uint32(rawHello[3]))
    ...
}
```

`layer4.Connection` is a peekable/replaying wrapper (bounded by `layer4.MaxMatchingBytes`), so
after a match the `proxy` handler re-sends the consumed prefix downstream verbatim — genuine
passthrough. The SNI/ALPN sub-matchers operate on `*tls.ClientHelloInfo`
(`modules/l4tls/sni_matcher.go`, `modules/l4tls/alpn_matcher.go`, module IDs
`tls.handshake_match.sni` / `tls.handshake_match.alpn`) — ClientHello fields, all cleartext in
the handshake, no decryption.

Distinction that matters: caddy-l4 has BOTH a `tls` **matcher** (peeks, this is what we want)
and a `tls` **handler** (actually terminates). Passthrough = use the matcher + a `proxy`
handler with **no `tls` handler** in the route.

**Exact Caddyfile syntax (verified against `docs/examples/*`):**

```caddyfile
@turn tls sni turn.enspyr.co
route @turn {
    proxy 127.0.0.1:5349        # raw TCP passthrough; LiveKit terminates TLS itself
}
```

`@turn tls sni turn.enspyr.co` peeks the ClientHello SNI without decrypting — confirmed.

---

## Q2 — Sharing :443 between TURN passthrough and Caddy's own HTTPS

**VERDICT: CONFIRMED, with a recommendation to change the approach.** There are two documented
patterns; the second is better for us.

### Pattern A — standalone `layer4` app owns :443, proxies non-matched to internal Caddy

This is the shape in the task proposal (Caddy moves to `127.0.0.1:8443`). It works
(`tls_sni_dynamic_upstreams.md`, `http_and_https_mix.md`) but has a sharp edge: caddy-l4's
own docs (`combining_apps.md`) **explicitly warn against a `layer4` block binding :443 while
the HTTP app also uses :443**:

```caddyfile
# from docs/examples/combining_apps.md — the WARNED-AGAINST shape
# tcp/:443 { ... }   # "will cause issues and won't work correctly,
#                    #  if the HTTP app listens to port 443"
```

So Pattern A requires Caddy's HTTP app to move off :443 entirely (e.g. `127.0.0.1:8443`) and
the layer4 app to proxy passthrough TLS to it. Caddy still terminates TLS + serves ACME certs
on :8443, but auto-HTTPS assumptions about :443 (esp. TLS-ALPN) are now wrong — see Q3.

### Pattern B — RECOMMENDED — Caddy keeps :443, `listener_wrappers` peels off TURN

caddy-l4 ships a `layer4` **listener wrapper** that plugs into Caddy's HTTP server. Caddy
still binds :443 natively; the wrapper intercepts each accepted connection, and only the
matched SNI is proxied out. Everything unmatched falls through to the next wrapper (`tls`) →
Caddy's normal TLS terminate + HTTP routing + ACME. Verbatim from
`docs/examples/combining_apps.md`:

```caddyfile
{
    servers :443 {
        listener_wrappers {
            layer4 {
                @tls tls
                route @tls {
                    subroute {
                        @tls-4 tls sni subdomain-4.example.com
                        route @tls-4 {
                            # proxy encrypted traffic to the backend  (NO `tls` handler = passthrough)
                            proxy {
                                upstream tcp/backend:443 { }
                            }
                        }
                        # ...unmatched TLS falls out of the subroute...
                    }
                }
            }
            tls          # <-- unmatched connections hit Caddy's native TLS terminator here
        }
    }
}
```

Applied to us (the whole design in ~10 lines):

```caddyfile
{
    servers :443 {
        listener_wrappers {
            layer4 {
                @turn tls sni turn.enspyr.co
                route @turn {
                    proxy 127.0.0.1:5349      # passthrough to LiveKit embedded TURN
                }
            }
            tls                                # chat/livekit SNIs + ACME land here, unchanged
        }
    }
}

chat.enspyr.co     { reverse_proxy ... }       # unchanged Caddy sites
livekit.enspyr.co  { reverse_proxy ... }
```

**Does Caddy's auto-HTTPS / cert serving still work unchanged under Pattern B? YES** — because
Caddy still owns :443 and unmatched TLS reaches its native `tls` wrapper. The *only* SNI that
never reaches Caddy's terminator is `turn.enspyr.co` (by design → LiveKit). That one exception
is the entire ACME subtlety in Q3.

---

## Q3 — ACME renewal behind the mux

**VERDICT: CONFIRMED mechanism; one real gotcha, cleanly avoidable (high confidence).**

Caddy defaults to trying **HTTP-01 (port 80)** and **TLS-ALPN-01 (port 443, ALPN
`acme-tls/1`)**, picking one **at random** per cert (Caddy docs, *Automatic HTTPS*). Port
requirements, quoted:

> HTTP Challenge: "requires port `80` to be externally accessible."
> TLS-ALPN Challenge: "requires port `443` to be externally accessible."

**(a) HTTP-01 / port 80 — UNAFFECTED.** This design touches only :443. :80 is not in the
layer4 front door at all, so HTTP-01 works exactly as today for every domain.

**(b) TLS-ALPN-01 / port 443 — the gotcha, scoped to ONE domain.**
- Under **Pattern B**, ACME TLS-ALPN handshakes for `chat.enspyr.co` and `livekit.enspyr.co`
  do NOT match the `turn.enspyr.co` SNI, fall through to Caddy's native `tls` wrapper, and
  validate normally. **No action needed for those.**
- The single hazard is **`turn.enspyr.co`'s OWN cert**: since *all* `turn.enspyr.co` SNI is
  passed through to LiveKit:5349, a TLS-ALPN-01 challenge for `turn.enspyr.co` would be routed
  to LiveKit (which does not speak `acme-tls/1`) and fail. Two clean fixes:

  **Fix 1 (simplest, recommended): rely on HTTP-01 for `turn.enspyr.co`.** Keep :80 open;
  optionally make it deterministic by disabling the TLS-ALPN challenge. JSON path (schema-verified):
  `apps/tls/automation/policies/issuers/acme/challenges/tls-alpn/disabled: true`. Caddyfile
  directive `disable_tlsalpn_challenge` exists **but** see caddy issue **#7612** ("TLS_ALPN
  challenge doesn't get disabled by Caddyfile directive") — *UNVERIFIED whether the Caddyfile
  directive is currently reliable*; prefer the JSON `disabled` field, or just leave both
  challenges on and lean on :80 (random selection will succeed via HTTP-01 whenever TLS-ALPN
  would route to LiveKit and fail... but "fail sometimes then retry" is ugly — disable ALPN
  for determinism).

  **Fix 2 (carve-out): route the ACME ALPN for turn to Caddy.** caddy-l4 has an `alpn`
  matcher (`docs`: `alpn <values...>`). Put a higher-priority route in the subroute:
  ```caddyfile
  @turn-acme tls sni turn.enspyr.co alpn acme-tls/1
  route @turn-acme { }                      # no proxy -> falls through to Caddy's tls wrapper
  @turn tls sni turn.enspyr.co
  route @turn { proxy 127.0.0.1:5349 }
  ```
  More moving parts than Fix 1; only needed if you insist on TLS-ALPN for turn.

**Operational note (separate from the challenge type):** LiveKit terminates the passthrough
TLS itself using Caddy's issued cert (proven working). Whichever challenge renews it, LiveKit
must **reload the renewed cert PEM** (LiveKit does not watch Caddy's cert store). Confirm the
existing 5349 deploy already handles cert refresh/reload, or renewal will silently serve a
stale cert to relayers after ~60–90 days. Flag for the design doc.

---

## Q4 — xcaddy rebuild + swap for a systemd Caddy, with instant rollback

**VERDICT: CONFIRMED, standard procedure (high confidence).**

- **caddy-l4 v0.1.2 pins `caddyserver/caddy v2.11.4` exactly** (its `go.mod`) — matches the
  box's stock Caddy v2.11.4. No version-skew risk. Build against that same Caddy version.
- **Go toolchain:** caddy-l4 v0.1.2 `go.mod` declares `go 1.25.1` → need **Go ≥ 1.25.1**
  installed for xcaddy. (xcaddy itself does not install Go.)

Build (on a builder box or the target; produces a static binary):

```bash
go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
xcaddy build v2.11.4 --with github.com/mholt/caddy-l4@v0.1.2
# -> ./caddy  (pin both versions for reproducibility)
./caddy list-modules | grep layer4      # sanity: layer4.* modules present
./caddy version                          # sanity: v2.11.4
```

Swap in for systemd `caddy` (typical `/usr/bin/caddy`, service `caddy`,
`/etc/caddy/Caddyfile`) with instant rollback:

```bash
# 1. keep the old binary for rollback
sudo cp /usr/bin/caddy /usr/bin/caddy.stock-2.11.4

# 2. validate the NEW config with the NEW binary BEFORE touching the running service
sudo ./caddy validate --config /etc/caddy/Caddyfile   # catches layer4 config errors, see below

# 3. install new binary
sudo install -m 0755 ./caddy /usr/bin/caddy

# 4. graceful reload (near-zero downtime; systemd unit uses `caddy reload`/SIGUSR1)
sudo systemctl reload caddy   ||   sudo systemctl restart caddy
systemctl status caddy --no-pager

# ROLLBACK (instant):
sudo install -m 0755 /usr/bin/caddy.stock-2.11.4 /usr/bin/caddy && sudo systemctl restart caddy
```

- **`caddy validate` DOES catch layer4 config errors** — layer4 modules are provisioned during
  config load, so a malformed `layer4`/`tls`/`proxy` block, unknown matcher, or bad upstream
  fails `validate` before any reload. Confidence: high (standard Caddy module lifecycle);
  cheap to confirm live (`caddy validate` on a deliberately-broken block).
- **Caddyfile format is fine** — caddy-l4 has full Caddyfile support (README: "This app
  supports Caddyfile"), incl. the global `layer4 { }` option and the `listener_wrappers`
  block used above. **No JSON conversion required.** (`caddy adapt` can dump the JSON if you
  want to inspect it.)
- **systemd hardening caveat:** the stock `caddy.service` often has `ProtectSystem=strict` /
  read-only `/usr`. `install` to `/usr/bin` may need the unit's protections relaxed or use
  `systemctl stop` first. Verify the unit's sandboxing on the box (cheap: `systemctl cat caddy`).

---

## Q5 — TURNS passthrough correctness + firewall

**VERDICT: CONFIRMED (high confidence).**

- **TURNS = TURN over TLS over TCP.** The advertised URI `turns:turn.enspyr.co:443?transport=tcp`
  means: client opens a **TCP** connection to :443, does a **TLS** handshake
  (SNI=`turn.enspyr.co`), then speaks the TURN protocol *inside* the TLS tunnel. Layer4 raw
  **TCP** passthrough is exactly correct: caddy-l4 peeks the cleartext ClientHello SNI, proxies
  the TCP stream (incl. that ClientHello) to LiveKit:5349, and LiveKit completes the TLS
  handshake + TURN allocation end-to-end with the client. `transport=tcp` is inherent to TURNS —
  no concern; TCP-level passthrough is the only correct level.
- **Firewall — NO new public ports needed; net change is a REDUCTION:**
  - `:443/tcp` — already open (Caddy). This becomes the *sole* public TLS-TURN ingress.
  - `:5349/tcp` — can/should be bound **localhost-only or firewalled from public** once layer4
    fronts it. LiveKit's TURN listener binds an address in `pkg/service/turn.go`
    (`net.Listen("tcp", net.JoinHostPort(addr, tls_port))`); constrain via config bind addr if
    supported, otherwise an iptables REJECT on public 5349. **NB — the OCI double-firewall
    gotcha applies on nick-mel/enspyr: closing the public 5349 needs the local iptables rule,
    not just the OCI security list.** (LiveKit still needs to *listen* on 5349 for the layer4
    proxy to dial `127.0.0.1:5349`.)
  - `:3478/udp` — unchanged (UDP TURN/STUN); orthogonal to this design.
  - **relay_range 50000–60000** — these are the TURN relay allocation ports. In the embedded
    setup the relay bridges client↔TURN↔SFU where the SFU is co-located, so relay traffic is
    largely localhost; leave the range as-is. *UNVERIFIED* whether any relay port must be
    publicly reachable for remote-peer media — cheapest check: run the existing forced-relay
    media proof after cutover and watch which ports bind/flow (`ss -lunp | grep -E '5000|6000'`).

---

## caddy-l4 production-readiness caveats

- **Explicitly experimental.** README: *"⚠️ This app is very capable and flexible, but is
  still in development. Please expect breaking changes."* Latest tag **v0.1.2** (pre-1.0).
- **Version pin is clean:** v0.1.2 pins Caddy **v2.11.4** — the exact stock version on the box.
  Pin `@v0.1.2` in the xcaddy build (don't float `@latest`) so a future breaking change can't
  surprise a rebuild.
- **Not an official Caddy-org repo** (mholt's personal), but it is *the* canonical layer4 module
  and widely deployed (OPNsense ships it as "Caddy Layer4 Proxy"). Mature enough for this use.
- **Blast radius:** this binary *is* your web front door (chat + WSS + video). Keep the stock
  binary for instant rollback (Q4) and validate before every reload.

---

## Cheapest experiments to close remaining unknowns

1. **`caddy validate` catches layer4 errors (Q4)** — build the binary, write a deliberately
   broken `layer4` block, `caddy validate` → expect non-zero exit. ~2 min.
2. **`turn.enspyr.co` cert renewal path (Q3)** — force a renewal (`caddy reload` after setting a
   very short cert lifetime on staging, or `caddy untrust`/delete the cert from storage) and
   confirm it renews via HTTP-01 with the mux live. Confirms Fix 1. ~10 min on staging.
3. **relay_range public-reachability (Q5)** — after cutover, run the existing forced-relay media
   proof and `ss -tulpn` on the box to see which relay ports actually bind/flow; decide the
   firewall for 50000–60000 from observation, not assumption. ~10 min (reuses task #2 harness).
4. **LiveKit cert reload after renewal (Q3 op-note)** — confirm the current 5349 deploy reloads
   LiveKit's cert when Caddy renews (grep the compose/systemd for a cert-watch or restart hook).
   Read-only, ~5 min.
5. **systemd sandbox on `caddy.service` (Q4)** — `systemctl cat caddy` to see
   `ProtectSystem`/`ReadOnlyPaths` before planning the binary swap. Read-only, ~1 min.

---

## Does the simpler alternative kill the layer4 design?

**NO — the simpler alternative does not exist.** The proposed "just make LiveKit advertise
`turns:5349`" relies on a config knob that is not in the code. Proof, `pkg/service/roommanager.go`
`iceServersForParticipant()`:

```go
if r.config.TURN.TLSPort > 0 {
    urls = append(urls, fmt.Sprintf("turns:%s:443?transport=tcp", r.config.TURN.Domain))
}                                          //          ^^^  443 is a string literal — no knob
```

`turn.tls_port` (5349) controls only the **listen** socket (`pkg/service/turn.go` binds
`turnConf.TLSPort`); the advertised port is the hardcoded `443`. `config-sample.yaml` says as
much operationally: *"tls_port … if not using a load balancer, this must be set to 443, as that
will be the port that's advertised to clients"* — i.e. the intended production setup is
tls_port=443, which we cannot do because Caddy owns 443. There is no `external_tls` variation
that changes the advertised `:443` (external_tls only toggles whether LiveKit terminates TLS or
expects an upstream L4 LB to; it still advertises tls_port-as-443).

The one genuinely-simpler *competing* design (not a config flip) worth pricing in Cast:
**give `turn.enspyr.co` its own public IP** (secondary VNIC/IP on the OCI instance) and bind
LiveKit `tls_port: 443` on that IP — then no Caddy rebuild, no layer4, no ACME carve-out. Cost:
a second public IP + a second ACME cert path + LiveKit bind-address config. If a second IP is
cheap on this box, it may beat rebuilding the front door. **Surface this fork in Cast; the
layer4 mux remains the right pick if the box is single-IP.**
