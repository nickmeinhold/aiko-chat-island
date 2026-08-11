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
| `e2e_media_relay.py` | acceptance gates A (connectivity) + B (exposure) |
| `e2e_relay_livekit.py` | gate A's engine: a livekit-rtc forced relay-only media client |
| `test/cert-tree-contract.sh` | contract test: fixture cert trees (host-FS vs docker-volume) |

## Acceptance gates (both required — see DESIGN §3.4, §4a)

- **A — connectivity:** a real **livekit-rtc** client (`e2e_relay_livekit.py`) forces
  relay-only ICE and confirms synthetic video round-trips — asserting
  `RESULT=RELAY_MEDIA_OK` **and** `all_relay=true` (every gathered candidate was a TURN
  allocation, so media had no path but the relay). This replaces the old
  `turnutils_uclient` gate: LiveKit's embedded TURN is **session-bound**, so there is no
  standalone TURN credential to hand turnutils — the client mints its cred by joining a
  room. Proves the **UDP/3478** relay path.
  - **KNOWN GAP — TLS/5349 relay is NOT asserted** (proven non-functional 2026-08-11):
    LiveKit advertises only the UDP TURN to clients (`turn.externalTLS:false`); with UDP
    blocked, a forced-relay client's peer connection times out. The `:5349` cert is
    valid — the relay *advertisement* is the gap. Tracked as the `external_tls` task; do
    not re-add a TLS assertion here until that proves TLS relay works.
- **B — exposure (before opening the range to real traffic):**
  - **B1** unauth `ALLOCATE` *positively* rejected (auth-reject marker, not just a
    non-zero exit) — `turnutils_uclient`, no cred needed for the negative test.
  - **B2** ports outside `50000–60000` sampled closed (advisory — can fail the gate,
    cannot certify closure; external multi-port audit still required).
  - **B3** no relay to RFC1918/link-local — asserted from the **LiveKit version**:
    v1.12.0+ denies restricted/private peer CIDRs by default (upstream changelog),
    read from `LIVEKIT_IMAGE` or `docker inspect livekit`. Replaces the old unwired
    `TURN_B3_PRIVATE_DENY_CMD` runtime probe.

`e2e_media_relay.py` **fails CLOSED**: any check that cannot produce positive evidence
exits non-zero and `standup.sh` will NOT open the firewall. Gate A additionally needs a
python with `livekit` + `livekit-api` + `numpy` (the box venv) as `python3`.
