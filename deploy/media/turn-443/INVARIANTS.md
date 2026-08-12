# TURN-on-443 mux — the invariant lattice (task #6, pre-rehearsal)

Enumerated **before** the empirical rehearsal, per
`feedback_enumerate_invariant_lattice_before_review`: four cage-match rounds discovered
these one at a time, and two of the P0s were fixes that regressed a *sibling* invariant
because the fix was checked against the finding in front of me, not against the set.

The surface is **four coupled state machines** — `iptables` (F), `livekit.yaml` (L),
`Caddyfile` (C), `HAProxy` (H) — that must transition together on a live shared `:443`.
Every invariant below is stated so it can be *tested*, not just asserted, and each names
the rehearsal that proves it.

| # | Invariant | Enforced where (layer) | Owning writer | Proving test |
|---|---|---|---|---|
| **INV-1** | Plaintext TURN is NEVER reachable on a public socket | F: v4+v6 `INPUT ! -i lo --dport 5349 DROP`; ordering (firewall **before** the plaintext flip); `netfilter-persistent` for reboot; rollback reopens **last** | `cutover.sh` 2.1 / `rollback.sh` final step | **Off-box** `openssl s_client <public-ip>:5349` refuses (v4, and v6 if a global v6 addr exists). A loopback probe CANNOT prove this. Rehearsal: probe from the VM host into the guest. |
| **INV-2** | `:443` is owned by exactly one process; the dark window is bounded + measured | H/C: `2.3` reload-Caddy → wait-for-release → `2.4` bind HAProxy; `port_listening` poll | `cutover.sh` 2.3/2.4 | Measure the window; assert never 2 owners and never a lasting 0-owner state. |
| **INV-3** | Every intermediate state is **boot-correct** (a reboot mid-cutover lands somewhere safe) | H: unit `disable`d in Phase 0, `enable`d at 2.3 *before* the live Caddy reload; F: persisted DROPs; L: `restart: unless-stopped` | `cutover.sh` (`systemctl enable/disable`, `netfilter-persistent save`) | **Reboot at each checkpoint** → assert (a) exactly one `:443` owner, (b) no public plaintext `:5349`, (c) chat still served. The highest-value test in the matrix. |
| **INV-4** | The turn cert stays renewable + served; the PEM is never absent; HAProxy stays **bootable** | C: `Caddyfile.mux` keeps a `turn.` block with `disable_tlsalpn_challenge` (HTTP-01 on `:80`); H: fingerprint-gated sync + graceful reload + `.needs-reload` sentinel; atomic rename `0640 root:haproxy` | `haproxy-cert-sync.sh` (**sole** writer of the PEM) | Force a renewal → served fingerprint == Caddy's store. Then fail the reload deliberately → PEM still present, `haproxy -c` still exit 0 (the r4 P0). |
| **INV-5** | Real client IP is preserved on the non-TURN path (the gateway's per-IP rate limiter) | H: `send-proxy-v2` + `check-send-proxy`; C: `:8443` listener reads PROXY from 127.0.0.1 | `haproxy.cfg` `be_caddy` + `Caddyfile.mux` | Hit chat through the mux from a distinct source IP; assert the gateway sees **that** IP, not 127.0.0.1. |
| **INV-6** | Rollback restores **all four** artifacts, in safe order, idempotently | ordering in `rollback.sh` (HAProxy off → Caddy back on `:443` → LiveKit back to TLS → **hard-gate** cert on 5349 → only then reopen firewall) | `rollback.sh`; `.stock` files staged + `cmp`-verified by `cutover.sh` Phase 1 | Roll back from **each** checkpoint; `cmp` all four against a pre-cutover snapshot. Run it **twice** (idempotency). Run it with a `.stock` missing/corrupt. |
| **INV-7** | SNI routing is correct and exposes nothing extra | H: `fe443` accepts only a real ClientHello, rejects the rest; `turn.` SNI → terminator; everything else → raw passthrough | `haproxy.cfg` `fe443` | Matrix: turn SNI → turn cert; chat SNI → chat cert + response; unknown SNI; **no** SNI; `acme-tls/1` ALPN; non-TLS junk (rejected); dribbled/slow hello (dropped at 5s). |
| **INV-8** | Caddy's `:8443` is not publicly reachable (it is bound on all interfaces **on purpose**, so `:80` HTTP-01 keeps working — the r3 P0) | F: v4+v6 DROP on 8443 | `cutover.sh` 2.1 | Off-box connect to `:8443` refused; loopback connect succeeds. |
| **INV-9** | No **false green** on the plaintext flip: LiveKit is genuinely running and holding `:5349` | L/H: bounded poll for Running + LISTENING, `ss` owner == `livekit-server`, then TLS-must-fail | `cutover.sh` 2.2 | Deliberately corrupt `livekit.yaml` → cutover must **roll back**, not proceed. (A dead socket also fails a TLS handshake — that was r-early's false-green.) |
| **INV-10** | The relay-deny policy survives the flip (`deny_peer_cidrs`, the task-#6 CGNAT fix) | L: the `awk` edit is scoped to the top-level `turn:` mapping | `cutover.sh` 2.2 awk | ✅ **PROVEN 2026-08-12** against production's real `livekit.yaml`: 2-line-out/1-line-in diff, `deny_peer_cidrs` intact, all other sections byte-identical, YAML re-parses. Behavioural half: B3 probe through the mux (RFC1918/CGNAT/loopback → 403). |

## Named, accepted gaps (not hidden)

- **The TURN path does not carry PROXY protocol** — LiveKit's embedded TURN isn't configured
  to expect it, so it sees the relay client as `127.0.0.1`. Allocation is unaffected; any
  LiveKit-side per-source-IP logic on the relay is blind. This is a **security tradeoff
  accepted by name**, not a throwaway "tax" — it belongs in an ADR line if it ships.
- **IPv6 is proactive**: the box has no global v6 today, so the `ip6tables` rules guard a
  universe that does not yet exist. If `ip -6 addr show scope global` ever becomes non-empty,
  the off-box v6 closure proof becomes a **blocking** sign-off step.

## Rehearsal matrix (what the VM run must cover)

Checkpoints, defined as the moments *between* the four transitions:

`CP0` pre-cutover · `CP1` after firewall · `CP2` after LiveKit flip · `CP3` after Caddy moves ·
`CP4` after HAProxy binds · `CP5` after cert-sync is wired

For each checkpoint: **reboot** (INV-3) and **rollback** (INV-6) are independent runs.
Plus the fault injections: corrupt `livekit.yaml` (INV-9), fail the cert reload (INV-4),
partial firewall apply (INV-1), missing `.stock` (INV-6).
