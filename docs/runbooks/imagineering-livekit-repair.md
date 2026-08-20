# Runbook — imagineering LiveKit (CLIENT + REPAIR)

**Design of record:** [`docs/crucible/14-media-companion-standup/DESIGN.md`](../crucible/14-media-companion-standup/DESIGN.md) (task #14).

imagineering's LiveKit is **shared box infra** — AITW (Firebase webhook + Redis
dispatch), dreamfinder, lyra, tech-world, `realm-token` mint all depend on it. It
is **owned outside the island**. The island is a *client*. So the island's
automation (`deploy/media/standup.sh`, `cert-restart.timer`) is **NOT installed
here** — repair is a **human-forced closed state machine**, run in a change
window with tenant notice. This runbook IS that control plane; "attended" without
it is folklore (the failure class that produced the Jul-24 dead cert).

- **Box:** `ssh imagineering` · LiveKit at `~/apps/livekit/` · `sudo -n docker`.
- **Shared-SFU owner (role):** island on-call — currently **Nick**; secondary: _____.
  Paging path: the box's notify proxy (`served-cert-alarm.sh` → Telegram).
- **A LiveKit restart is a full multi-tenant media outage** (embedded TURN shares
  the SFU process). Every restart below drops in-progress calls for ALL tenants
  until reconnect. Until the live-room measurement (DESIGN §4 step 6) lands,
  **assume worst-case full outage** when setting notice lead time.

---

## One-time: fix the fire + bring under managed cert (the 18-day-dead cert)

The cert at `~/apps/livekit/certs/turn.imagineering.cc.crt` expired **Jul 24** and
is hand-managed (no renewal). Bring it under Caddy issuance + bind-mount:

1. **Add the Caddy cert block** for `turn.imagineering.cc` (container Caddy at
   `~/apps/caddy/Caddyfile`) so Caddy issues + auto-renews via HTTP-01:
   ```
   turn.imagineering.cc {
       respond "turn" 200
   }
   ```
   Confirm `turn.imagineering.cc` has an A-record → the box and port 80 is
   reachable. Reload Caddy; wait for the cert under
   `/data/caddy/certificates/acme-v02…/turn.imagineering.cc/`.
2. **Bind-mount the LEAF cert dir RO** into the livekit container (NOT the whole
   Caddy volume — it holds the ACME account key). Point `livekit.yaml`'s
   `turn.cert_file`/`key_file` at the mount. Add `turn.relay_range_start: 50000` /
   `relay_range_end: 60000` if absent. **Do NOT change** the api keys, webhook, or
   redis — those are load-bearing for AITW.
3. **Announce** the window to tenant owners (dreamfinder/lyra/AITW). Then in the
   window: `sudo docker compose up -d` (or `restart livekit`) to pick up the mount.
4. **Exposure gate B** (`deploy/media/e2e_media_relay.py --exposure-only`) while
   the relay range is still constrained, **then** open the firewall (OCI
   security-list + host iptables: UDP 3478, TCP 5349, UDP 50000-60000).
5. **Connectivity gate A** + one non-island **canary** (a dreamfinder or lyra
   call) to confirm the shared plane is healthy post-change.
6. **Install the served-cert alarm as DETECTOR-ONLY** (`ALARM_RESTART=0`) — it
   pages, it does not restart. The restart stays human-forced (below).

Separately, **pin the image** off `:latest` → `v1.13.5` as its OWN windowed step
(a pin is a TURN-auth behavior change): enumerate the TURN-token issuers first
(`realm-token` mint, clients, agents), then bump, restart-in-window, re-run gate A.

---

## Steady-state: the closed renewal loop (run every renewal)

```
  DETECT ─▶ PAGE ─▶ NOTICE ─▶ ACT ─▶ RE-GATE ─▶ (close)
     └───────────────── ESCALATE if no window within T ◀──┘
```

1. **DETECT** — `served-cert-alarm.sh` probes the **:5349 endpoint** (not the disk
   file — pion/turn serves the in-memory cert) and pages on `notAfter < N days`
   (N=14, chosen so a full window fits before expiry). Caddy has already renewed
   on disk; LiveKit is still serving the old cert.
2. **PAGE** — the alarm pages the **role** (island on-call), not a person. If
   unacked within the secondary-page interval, page the secondary.
3. **NOTICE** — post the tenant-notice (dreamfinder/lyra/AITW owners) with the
   window + lead time (worst-case-full-outage severity until §4-step-6 measured).
4. **ACT** — in the window, first **validate the on-disk pair without touching the
   service**: `./cert-restart.sh --validate-only` (parses the leaf pair, EC+RSA
   agnostic, never restarts/probes — the safe command for shared infra). If VALID:
   `sudo docker restart livekit`. Do NOT run bare `cert-restart.sh` here — that is
   BOOTSTRAP automation; on this shared box the restart is yours to fire in-window.
5. **RE-GATE** — re-run gate A + the non-island canary; confirm the alarm now
   reads a fresh `notAfter`.
6. **ESCALATE** — if no change-window is taken within **T** of the first page, the
   alarm re-fires and missed-window is itself escalated — so the loop cannot
   silently decay into an unowned cert death.

**Rollback:** if a restart lands on a bad cert or breaks a tenant, revert to the
prior image digest / prior cert and restart; the mount makes the cert side
declarative.

**Reserved alternative (do NOT build yet):** if this attended loop's operator cost
becomes the dominant ops tax, build the hot-reload TLS terminator on :5349 (DESIGN
§6) so renewals stop requiring an SFU restart. That is restart-decoupling, not
coturn.
