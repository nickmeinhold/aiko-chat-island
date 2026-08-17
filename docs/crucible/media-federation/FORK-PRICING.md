# Pricing the media-relay fork (#3196)

*Read from source, 2026-08-17. `livekit/livekit@b0e2d89` (2026-08-14) and
`jitsi/jitsi-videobridge@5380ebc` (2026-08-05), both cloned and measured — not summarised from
docs. Commissioned by Nick: "go read the source and price the fork."*

**Answer up front: the media plumbing is small and the integration is not. The permanent cost is
not writing the relay — it is that 56% of upstream commits land in the exact code you patch.**

---

## 1. What a relay actually costs to write — the reference implementation

Jitsi already ships the thing we would be building. Its entire relay package:

| File | LOC |
|---|---|
| `relay/Relay.kt` | 1,214 |
| `relay/RelayMessageTransport.kt` | 528 |
| `relay/RelayedEndpoint.kt` | 263 |
| `relay/RelayEndpointSender.kt` | 185 |
| `relay/RelayConfig.kt` | 46 |
| `relay/RelayedPacketInfo.java` | 39 |
| `relay/AudioSourceDesc.kt` | 28 |
| **Total** | **2,303** |

**2,303 lines is a complete, production, cross-bridge media relay.** That is the honest size of
the media-forwarding problem, and it is *not* the scary number. It is also a working blueprint we
can read — the wire format is not published anywhere, but the source is right there.

## 2. Where LiveKit's seams actually are

The good news first. `pkg/sfu/interfaces.go` is **189 lines total**, and the forwarding path
targets narrow interfaces:

| Interface | Methods | Role for a relay |
|---|---|---|
| `TrackSender` | **14** | the sink — a relay implements this to *receive* forwarded media |
| `TrackReceiver` | **27** | the source — a relay implements this to *inject* remote media |

That is genuinely tractable, and it is exactly what LiveKit's own engineering blog means by
*"the relay destination is treated like just another participant in the send loop."* At the SFU
layer, that sentence is true.

**One layer up it stops being true, and that is the whole cost.**

| Thing | Measure | Consequence |
|---|---|---|
| `types.LocalParticipant` | **136 methods**, ~490 lines of interface | the participant seam is a god-interface, not a thin sink |
| generated fake for it | **10,212 lines** | the size of the fake is the tell |
| `ParticipantImpl` | 4,450 LOC, and `grep` confirms it is the **only** production implementation | nothing else has ever satisfied this interface |
| `Room` | **concrete struct**, 2,427 LOC — not an interface | you cannot substitute a room; you must modify it |
| `Room.GetParticipants()` | returns `[]types.LocalParticipant` | every caller assumes participants are local |

So to make a remote bridge's participants visible to local subscribers, there are two doors and
both are inside the blast radius:

- **(a)** implement `LocalParticipant` — 136 methods, for an object that has no ICE agent, no
  signalling connection, and no publisher peer connection. Most of it would be stubs, and stubs
  on a 136-method interface are where the bugs live.
- **(b)** modify `Room` to hold a second collection of remote participants — which is precisely
  what LiveKit Cloud does (`Session{ localParticipants, remoteParticipants }`, per their blog),
  and that type is **Cloud-side, not in OSS**. It means surgery on `room.go` plus every call site
  that assumes `GetParticipants()` is local.

LiveKit chose (b) and kept it closed-source. We would be re-deriving their private design against
their public code.

### What OSS already has, and what it doesn't

`pkg/routing/signal.go` has `RelaySignal`, `RelaySignalRequest`, `RelaySignalResponse` over PSRPC.
Reading it: this relays **client signalling** to the node hosting the room. It is *not* media
relay, and it does not help — it exists so any node can accept a connection for a room that lives
elsewhere. The room's media still lives on exactly one node ("for now, a room must fit on a single
node").

## 3. The number that decides this — upstream churn in the patched surface

The fork surface is `pkg/sfu` + `pkg/rtc` = **87,188 LOC**.

Measured over the last six months on `main`:

| Metric | Value |
|---|---|
| Commits touching `pkg/sfu` or `pkg/rtc` | **155** |
| Total commits | 279 |
| **Share landing in our patched surface** | **56%** |
| Lines changed in that surface | **+14,242 / −3,495** |
| Minor releases shipped | **6** (v1.9.12 → v1.13.5) |

That is ~26 commits and ~2,400 changed lines per month, in the exact files a relay patch must
live in. **We run v1.13.5 today.** Every upgrade becomes a rebase of a media-plane patch against
a moving target, and a botched rebase in an SFU does not fail loudly — it degrades a call.

This is the permanent cost, and it does not decay. It is also the cost that does not appear in
any estimate of "how long to build the relay."

## 4. The part with no prior art

Both Octo and LiveKit's mesh solve **cascading**: one operator, many boxes, mutually trusting.
Jitsi's own documentation is explicit that Octo *"does not have its own security mechanism"* and
requires bridges on *"a secure network."* Newer JVB moved the relay to
`wss://…/colibri-relay-ws/…?pwd=`, so it has improved, but the architecture still assumes a single
`jicofo` coordinating bridges one operator owns.

**Nobody ships cross-operator SFU federation.** So the relay mechanics are the *known* part that
we can read from JVB, and the trust boundary between independently-operated islands — the part
aiko specifically needs — is the part we would invent from nothing. It is also a trust boundary,
so it is cage-match-by-law, and the threat model ("what can a hostile or compromised peer island
do to a gathering it relays for?") does not exist yet in any form.

## 5. Estimate

Stated as a prediction with its basis, not a quote. Anchored on this partnership's demonstrated
numbers — the DM increment took a 13-round cage-match, the actuation signer took 9, and the
media-plane work (TURN-on-443) took a disposable-VM rehearsal plus 10 rounds before it was
trustworthy. Media and trust boundaries have both historically cost us more rounds than
estimated, and this is both at once.

| Phase | Estimate | Confidence |
|---|---|---|
| Relay transport + `TrackSender`/`TrackReceiver` adapters | 2–3 weeks | reasonable — narrow seams, JVB blueprint |
| Room-level remote-participant integration (the invasive part) | 3–4 weeks | **low** — concrete `Room`, 136-method interface, no OSS precedent |
| Cross-operator auth + threat model + cage-match | 2–3 weeks | **low** — no prior art |
| Hardening: simulcast selection across the link, packet loss, reconnect, bandwidth estimation | **open-ended** | this is the phase that historically eats teams |
| Rebase tax | ~26 commits/month against our patch, forever | measured, not guessed |

**~2–3 months to something demonstrable, an unbounded tail after it, and a permanent fork.**

The honest risk marker: phase 4 has no upper bound I can defend. Simulcast layer selection across
a relay link is called out in the webrtcHacks write-up as an unsolved inefficiency in Octo *after
years of production use by the people who invented it.*

## 6. Recommendation

Unchanged by the source read, but now priced rather than asserted.

**Build the cross-island authorization first. Both paths need it and it is the novel part.**

- **Host-election** ships on it immediately: a gathering elects one island's SFU, remote
  participants get tokens for it, verified against their home island's signing key (#1816,
  `/v1/keys`, migration 0011). No fork, no rebase tax, weeks not months, and it delivers the
  product property — *a gathering can span islands*.
- **The relay, if still wanted, needs that same auth layer** to trust a peer bridge. Nothing is
  thrown away by doing it first. You would be adding media relay to a working federated-auth
  system instead of inventing both at once.

What I would avoid is starting at the fork: it front-loads the permanent maintenance burden *and*
the only genuinely unsolved piece, simultaneously, for a capability no two humans have exercised
once.

**If the answer is still "build it", the next step is not code.** It is a threat model for the
inter-island relay link, because that is the part nobody has done and the part that decides
whether the design is even safe to run between boxes different people own.

---

### Reproducing these numbers

```bash
git clone --depth 1 https://github.com/livekit/livekit.git && cd livekit && git fetch --deepen=2000
wc -l jvb/src/main/kotlin/org/jitsi/videobridge/relay/*.kt   # 2,303 total relay
awk '/^type LocalParticipant interface \{/,/^\}/' pkg/rtc/types/interfaces.go | grep -cE '^\s+[A-Z][A-Za-z0-9_]*\('   # 136
awk '/type TrackSender interface \{/,/^\}/' pkg/sfu/interfaces.go            # 14 methods
git log --since="2026-02-17" --oneline -- pkg/sfu pkg/rtc | wc -l            # 155
git log --since="2026-02-17" --oneline | wc -l                               # 279
git log --since="2026-02-17" --numstat --format="" -- pkg/sfu pkg/rtc | awk '{a+=$1; d+=$2} END {print "+"a" -"d}'
```
