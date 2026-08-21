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
| `webrtc_relay_proof.py` | a real **Chromium** client proving the **TURNS :443** relay carries a call |
| `test/cert-tree-contract.sh` | contract test: fixture cert trees (host-FS vs docker-volume) |

## Acceptance gates (both required — see DESIGN §3.4, §4a)

- **A — connectivity:** a real **livekit-rtc** client (`e2e_relay_livekit.py`) forces
  relay-only ICE and confirms synthetic video round-trips — asserting
  `RESULT=RELAY_MEDIA_OK` **and** `all_relay=true` (every gathered candidate was a TURN
  allocation, so media had no path but the relay). This replaces the old
  `turnutils_uclient` gate: LiveKit's embedded TURN is **session-bound**, so there is no
  standalone TURN credential to hand turnutils — the client mints its cred by joining a
  room. Proves the **UDP/3478** relay path.
  - **The old TLS/5349 KNOWN-GAP note is SUPERSEDED** (was: "proven non-functional
    2026-08-11, do not re-add a TLS assertion"). Both boxes advertise
    `turns:<domain>:443?transport=tcp` alongside the UDP TURN, and a real Chromium client
    completes a relay-only call over it — measured 2026-08-22 on **both** islands. The
    port in the gap note was also wrong for the client-facing path: LiveKit advertises
    `:443` regardless of the configured `tls_port` (enspyr's is still `5349`).
    `webrtc_relay_proof.py` is the assertion; see below.

- **A2 — TLS relay (`webrtc_relay_proof.py`):** Chromium, relay-only, with the session's
  ICE servers filtered to `turns:` at the `RTCPeerConnection` boundary so the TLS arm is
  forced without needing a UDP-blocked vantage. Mint a join token from the box's
  `LIVEKIT_API_KEY`/`SECRET`, then:

  ```sh
  LK_URL=wss://livekit.<domain> LK_TOKEN=<minted> TURN_DOMAIN=turn.<domain> \
    RELAY_ARM=turns ./webrtc_relay_proof.py          # expect exit 0
  RELAY_NULL_PORT=4443 ... ./webrtc_relay_proof.py   # NULL CONTROL: expect exit 3
  ```

  **Always run the null arm.** A green from an instrument whose failure mode has never
  been observed is not evidence. Exit codes: `0` proven, `3` failed, `4` INCONCLUSIVE
  (the CDN-hosted SDK never loaded — an instrument failure, not a server verdict).

  **Known flake:** 6 of 7 relay-only connects succeeded against imagineering on
  2026-08-22; one failed at `connect` and did not reproduce over 4 immediate retries. Any
  gate built on this probe (#3055) must retry with attempt accounting rather than treat a
  single red as a verdict.
- **B — exposure (before opening the range to real traffic):**
  - **B1** unauth `ALLOCATE` *positively* rejected (auth-reject marker, not just a
    non-zero exit) — `turnutils_uclient`, no cred needed for the negative test.
  - **B2** ports outside `50000–60000` sampled closed (advisory — can fail the gate,
    cannot certify closure; external multi-port audit still required).
  - **B3** no relay to RFC1918/link-local — a **behavioral, packet-level probe**
    (`b3_relay_probe.py`), not a version/config proxy. It extracts the SFU's
    session-bound TURN credential from the raw signaling `JoinResponse` (which
    `livekit-rtc` never surfaces — the wire does), allocates a relay, then issues
    `CreatePermission` for a **public control** + the **SSRF-critical private ranges**
    (`10/8`, `172.16/12`, `192.168/16`, `169.254/16`, `127/8` — one **sentinel** per
    range, a mandatory hard-coded set that env can only add to, never shrink),
    requiring the control **allowed (200) before and after** and every sentinel
    **refused (403)**. Sampled sentinels, not an exhaustive range proof. **Every
    advertised relay endpoint (each transport — udp/tcp/tls) is tested on one
    allocation**; a live one must pass, an **unreachable** one (can't allocate → not a
    relay vector) is surfaced non-blocking, and ≥1 live endpoint must pass. Known-open
    bands (`100.64/10` CGNAT) and unreachable endpoints are named in the `B3_ASSERT`
    verdict, not hidden. Fail-closed exit code
    (0 OK / 3 FAIL / 2 BLOCK) **and** a single structured `B3_ASSERT` verdict line.
    Supersedes the prior version-proxy — and is more truthful: it found LiveKit's
    default deny does **not** cover `100.64/10` (CGNAT), tracked separately (task #6).
    Runs in the exposure phase: needs `:443` (join) + `:3478` (TURN control), not the
    relay range.

`e2e_media_relay.py` **fails CLOSED**: any check that cannot produce positive evidence
exits non-zero and `standup.sh` will NOT open the firewall. Needs the box venv as
`python3`: `livekit` + `livekit-api` + `numpy` (gate A) + `websockets` + `aioice`
(gate B3).
