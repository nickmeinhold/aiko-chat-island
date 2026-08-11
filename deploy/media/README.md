# deploy/media — LiveKit media plane (SFU + embedded STUN/TURN)

**Design of record:** [`docs/crucible/14-media-companion-standup/DESIGN.md`](../../docs/crucible/14-media-companion-standup/DESIGN.md) (task #14, tempered 3-round crucible).

## The frame (read this first)

**The media plane is always BOX-plane, never island-plane.** The island
(gateway + broker + registrar + ChatServer) is one *consumer* of it. There is no
separate coturn — STUN/TURN live *inside* LiveKit (pion/turn).

Two operator postures, drawn on the **ownership** line:

| | BOOTSTRAP | CLIENT + REPAIR |
|---|---|---|
| **When** | the box has no media plane yet (enspyr, any greenfield island) | a media plane already exists, shared by other tenants (imagineering: AITW/dreamfinder/lyra/token-mint) |
| **Who owns lifecycle** | the island | someone else; the island is a *client* |
| **This dir** | **authoritative** — stand up + operate from here | **reference only** — repair via [`docs/runbooks/imagineering-livekit-repair.md`](../../docs/runbooks/imagineering-livekit-repair.md) |
| **Renewal restart** | **machine-forced** (unattended timer) — blast is island-only | **human-forced** closed state machine (attended window + tenant notice) — island automation NEVER autonomously restarts a shared SFU |

⚠️ **This dir ships BOOTSTRAP only. A shared multi-tenant SFU is out-of-repo
ownership — do NOT point `standup.sh` at imagineering.** If a second shared box
ever appears, it gets a runbook, not this automation.

## Why a restart at all

pion/turn loads its TLS cert **once at process start** (no hot-reload,
[livekit#3463](https://github.com/livekit/livekit/issues/3463)). Caddy issues +
auto-renews the `turn.<domain>` cert; we **bind-mount that leaf cert dir
read-only** into LiveKit (no copy, no dual source-of-truth), and a **restart** is
the only way to pick up a renewal. A LiveKit restart is a **real multi-tenant
media outage** (embedded TURN shares the SFU's process) — priced as an outage
everywhere, not a nick.

## Files

| File | Role |
|---|---|
| `docker-compose.yml` | pinned `livekit-server:v1.13.5`; leaf cert dir bind-mounted RO |
| `livekit.yaml.tmpl` | rendered to `livekit.yaml`; turn on, narrow relay range, disjoint ICE range |
| `.env.example` | per-box values (real `.env` stays on the box, never committed) |
| `standup.sh` | BOOTSTRAP: render → first-cert-before-turn → exposure test (B) → open firewall → connectivity test (A) |
| `update.sh` | pull the pinned image, backup-first, `up -d`, verify |
| `served-cert-alarm.sh` | probes the **:5349 TLS endpoint** (not the disk file) for `notAfter < N days` → alert |
| `cert-restart.sh` + `.service`/`.timer` | BOOTSTRAP restart trigger (alarm-driven) |
| `e2e_media_relay.py` | acceptance gate A (relay over TCP/TLS + UDP canary) |
| `test/cert-tree-contract.sh` | contract test: fixture cert trees (host-FS vs docker-volume) |

## Acceptance gates (both required — see DESIGN §3.4, §4a)

- **A — connectivity:** forced-relay (`iceTransportPolicy: relay`) selects a
  candidate with `type == relay` **AND** `protocol == TCP/TLS`; plus a UDP-relay
  canary. `e2e_media_relay.py`.
- **B — exposure (before opening the range to real traffic):** unauth `ALLOCATE`
  fails · short-TTL LiveKit-issued creds · no relay to RFC1918/link-local · ports
  outside `50000–60000` proven closed · token-issuer allowlist · abuse ceiling.
