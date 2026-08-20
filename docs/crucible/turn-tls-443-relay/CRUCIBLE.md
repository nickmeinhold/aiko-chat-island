# CRUCIBLE — TURN-over-TLS on :443 (enspyr media plane)

**Ore locked (consent gate crossed):** task #4 — the dead TLS TURN relay. Scoped to
**enspyr only** for the first implementation. imagineering (~35 unrelated prod services
behind its 443) is a separate, later, explicitly-gated change.

## The spark

We already *have* a working TURN-over-TLS engine. This morning's behavioral probe
proved LiveKit's embedded TURN allocates a real relay over TLS on `:5349` on both
islands — valid cert, real relay address handed back. The capability exists and runs.

The only thing broken is a **wire**: the SFU tells joining clients to reach TURN/TLS at
`turns:turn.enspyr.co:443`, but `:443` is Caddy, and Caddy answers that hostname with a
literal `respond "turn" 200` HTTP stub. The client finishes its TLS handshake, sends its
first STUN Allocate, and Caddy's HTTP parser sits there waiting for `GET / HTTP/1.1` that
never comes. Timeout. A firewall-hostile client (UDP blocked, 5349 blocked, only 443 out —
the exact population TURN/TLS exists for) is pointed at a door that was never connected to
the room behind it.

**Why this thrills me:** the fix is *connecting a wire to an engine that already works*,
not building an engine. The satisfying inversion — three cheaper proxies (config says
5349, openssl says the cert is valid, "5349 is dead" was my morning assumption) each agreed
with a wrong story until the behavioral probe, which speaks the exact protocol a client
speaks, disagreed and located the break to a single stub line. That's the whole case for
behavioral gates over config gates, demonstrated in one morning.

**What it changes:** turns the island's video from "works for most networks" into "works
from behind a corporate firewall" — the difference between a demo and something you can
tell someone to just use at work. It's the last reachability gap in the media plane the
#14 media-companion standup stood up.

## The falsifier (what would prove this ore is slag)

**If LiveKit is advertising `:443` because of a config knob we can flip to advertise
`:5349` instead, the entire layer4 mux is unnecessary machinery** — the plain fix is a
one-line advertised-port change (stopgap "a"). The Heat phase MUST answer "why is LiveKit
advertising 443 when tls_port is 5349, and can we change the advertised port?" before we
commit to the layer4 design. If that knob exists AND 5349-through-firewalls is acceptable
coverage, we ship the one-liner and close this. The layer4 design only earns its blast
radius if (i) 443 is genuinely the right port for hostile-network reachability AND (ii)
LiveKit won't serve TURN/TLS on 443 itself without colliding with Caddy.

Second falsifier: if caddy-l4 SNI passthrough can't preserve the client's end-to-end TLS
to LiveKit :5349 (i.e. it must terminate TLS), the design changes shape (Caddy would need
LiveKit's cert, or LiveKit would need plaintext behind the mux) — Heat must confirm pure
ClientHello-peek passthrough.

## Blast radius (named up front — cage before monster)

This changes the **live :443 listener** that carries WSS signaling for **both chat and
video** on a production island. The layer4 app becomes the front door for ALL 443 traffic.
Max blast radius: a bad route dark-outs chat.enspyr.co + livekit signaling, not just video.
Ceremony: design → cross-family cage-match on the design → staged enspyr-first deploy with
an instant binary rollback. **Acceptance gate:** `b3_relay_probe.py` off-box vs enspyr
flips `turns:443` UNREACHABLE → ALLOCATED, with UDP relay + RFC1918-refusal intact AND
chat + livekit WSS still fully working.
