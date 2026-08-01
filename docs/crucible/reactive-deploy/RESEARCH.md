# Reactive island deploy — technical research (safety spine)

Feeds the `reactive-deploy` design. Goal: a systemd-timer watcher on each island box
polls GHCR for a new image **digest** on a release channel, and on change runs the
existing `deploy/update.sh` (backup-DB-first → `docker compose pull` → `up -d` → verify
`/health` on `127.0.0.1:8095`), auto-rolls-back to the previous digest if `/health`
fails, and notifies.

**Target facts.** Image: `ghcr.io/nickmeinhold/aiko-chat-island` (PUBLIC — anonymous
pull token works, verified below). Multi-arch (linux/amd64 + linux/arm64, built by
buildx so the index also carries `unknown/unknown` attestation manifests). Compose v2.
Two hosts: one bare `docker`, one `sudo -n docker`. `edge` tracks `main`; `vX.Y.Z` tags
are releases.

Everything marked **[verified]** was run live against the real registry / a real docker
daemon (Docker Engine 29.5.2, containerd image store) on 2026-08-01. **[uncertain]**
flags a claim I could not fully pin down — treat as an open question for the cage-match.

---

## 1. Query a GHCR tag's current digest without pulling and without a stored credential

GHCR requires a bearer token even for public pulls, but you can mint an **anonymous**
one — no username, no PAT. The Distribution token dance:

```bash
REPO="nickmeinhold/aiko-chat-island"
CHANNEL="edge"   # or vX.Y.Z

# 1. Anonymous pull token (public repo → no Basic auth needed)
TOKEN=$(curl -fsSL \
  "https://ghcr.io/token?service=ghcr.io&scope=repository:${REPO}:pull" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# 2. HEAD the manifest; read the index digest from the response header.
#    The Accept header is load-bearing: it selects WHICH digest you get back.
DIGEST=$(curl -fsSI \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
  "https://ghcr.io/v2/${REPO}/manifests/${CHANNEL}" \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}')
echo "$DIGEST"   # sha256:...
```

**[verified]** Live run returned `HTTP/2 200`,
`content-type: application/vnd.oci.image.index.v1+json`,
`docker-content-digest: sha256:b02b32645661efc1b444e208f094a232850a828b2fe48fac24e37fccfbe53f37`
for `edge`. So the anonymous dance works with zero stored credentials.

**Why `Accept` matters (the crux for "has the release changed?").** The registry does
content negotiation. If you send an `Accept` that matches the multi-arch **image index**
(`application/vnd.oci.image.index.v1+json`, or the older Docker
`manifest.list.v2+json`), `Docker-Content-Digest` is the **index digest** — the single
value the tag points at, and the value that changes when a new multi-arch release is
pushed. If you send a per-arch `Accept` (or none, and the registry picks one), you can
get a **per-arch manifest digest** instead, which is the wrong thing to diff against a
tag. **Always send the index `Accept`.** GHCR (and buildx builds) use the OCI
`image.index` media type; sending both index Accepts covers OCI + legacy Docker.

- **HEAD vs GET:** `HEAD` returns the same `Docker-Content-Digest` header without the
  body — cheaper, and it's what you want for a poll. The digest header is the registry's
  own canonical hash of the manifest, so you don't compute it yourself.
- **Token TTL:** the anonymous token is short-lived (minutes). Mint a fresh one each poll
  — do **not** cache it across timer runs.
- **Rate limits:** GHCR does not publish Docker-Hub-style hard anonymous pull caps, but
  treat the registry as rate-limited on principle. A poll every 1–5 min is fine; don't
  hammer.

### `docker`-native equivalents

```bash
# buildx imagetools — prints the index + all child platform digests; most robust.
docker buildx imagetools inspect ghcr.io/$REPO:$CHANNEL --raw | sha256sum   # NOT the digest; see below
docker buildx imagetools inspect ghcr.io/$REPO:$CHANNEL                     # human table, shows Digest: sha256:<index>

# manifest inspect — needs the digest parsed out; enable experimental in older docker.
docker manifest inspect --verbose ghcr.io/$REPO:$CHANNEL
```

- `docker buildx imagetools inspect <ref>` (no `--raw`) prints a `Digest:` line that **is
  the index digest** and a `Manifests:` list of the per-platform children — the cleanest
  single command, and it uses the daemon's auth so it works anonymously on a public repo.
  **[uncertain]** the exact scriptable field name across buildx versions — parse the
  `Digest:` line, or use `--format '{{.Manifest.Digest}}'` (Go-template support exists in
  recent buildx but pin/​test it on the box).
- `docker manifest inspect` is marked experimental in some engines and its output is the
  manifest body, not a bare digest, so you must hash/parse — clunkier in a script.

**Recommendation for the watcher: use the raw `curl` + HEAD dance.** It has no daemon
dependency, no experimental-flag surprises, is trivially scriptable (one header read),
and is the same mechanism regardless of the bare-`docker` vs `sudo -n docker` host split.
Keep `docker buildx imagetools inspect` as the human debug command.

---

## 2. Read the digest the running service is currently on

You are comparing the **registry index digest** (§1) against **what the box actually
deployed**. There are two local values and only one of them is the right comparand.

```bash
# The image ref the running compose service resolved to:
docker compose images chat-island --format json   # v2: array with .ID / .Repository / .Tag

# RepoDigests of the image the service is using (the useful one):
docker inspect --format '{{index .RepoDigests 0}}' \
  "$(docker compose ps -q chat-island)" 2>/dev/null
# or directly on the image:
docker image inspect ghcr.io/$REPO:$CHANNEL --format '{{json .RepoDigests}}'
```

### The index-vs-manifest footgun (this is the real trap)

A multi-arch tag is an **image index** (a list) whose digest is what the tag points to.
When docker **pulls** that tag on, say, amd64, it downloads the amd64 **child manifest**
and that child has its own, different digest.

**[verified]** For `ghcr.io/nickmeinhold/aiko-chat-island:edge` the index digest is
`sha256:b02b3264…`, while the child manifests inside it are:

| platform        | media type                                   | digest            |
|-----------------|----------------------------------------------|-------------------|
| linux/amd64     | oci.image.manifest.v1+json                   | `sha256:425dfbe0…`|
| linux/arm64     | oci.image.manifest.v1+json                   | `sha256:358b6c99…`|
| unknown/unknown | oci.image.manifest.v1+json (buildx attest.)  | `sha256:63a6a185…`, `sha256:ce2d7d8e…`|

If you compare the registry **index** digest (`b02b3264…`) against a locally-derived
**per-arch** digest (`425dfbe0…`) they will **never** match and the watcher will fire an
infinite redeploy loop. Two things must be true: compare index-to-index, and **do not
use `.Id`**.

- `.Id` on the **classic graphdriver** store is the local image **config** digest
  (per-arch) — it does **not** equal the registry index digest.
- `.RepoDigests` holds `repo@sha256:<digest>` **for the digest you pulled by**. When you
  `docker pull`/`compose pull` a multi-arch tag, docker pulls *by the tag* and records
  the **index** digest here. **[verified]** on Docker 29.5.2 (containerd image store):
  pulling `alpine:latest` (multi-arch) gave
  `RepoDigests=["alpine@sha256:28bd5fe8…"]` which matched the registry index digest
  exactly. `.Id` also happened to equal the index digest on the containerd store — but
  that equality is a **containerd-store quirk and is NOT portable to the classic store**,
  so never rely on `.Id`.

### Recommended robust comparison

Belt-and-suspenders, order of preference:

1. **Author your own source of truth.** At the end of a successful deploy, write the
   deployed index digest to a state file the watcher owns, e.g.
   `/var/lib/aiko-island/deployed.digest`. Each poll compares registry-index-digest
   (§1) against that file. This sidesteps every store-quirk and RepoDigests edge case —
   you control both sides of the comparison and they're both index digests by
   construction. **This is the primary recommendation.**
2. **Cross-check against the running container's `.RepoDigests[0]`** as a sanity guard,
   so a hand-run `update.sh` (that didn't write the state file) is still detected.
   Extract the `@sha256:` part and compare.
3. Never compare against `.Id`, and never compare an index digest against a per-arch
   digest.

---

## 3. Rollback: capture current digest before pull, restore after failed health check

The previous image is still in local storage the instant after a pull (a pull adds; it
doesn't replace), **provided it hasn't been GC'd** (§4). So rollback = re-`up -d` the
service pinned to the old digest — a purely local operation, no network.

### Pinning a compose service to an exact digest

The clean, robust mechanism is a **generated override file** that pins `image:` to a
digest, layered on top of the base `compose.yaml`:

```yaml
# /path/rollback.override.yml  (generated at rollback time)
services:
  chat-island:
    image: ghcr.io/nickmeinhold/aiko-chat-island@sha256:<OLD_INDEX_DIGEST>
```

```bash
docker compose -f compose.yaml -f rollback.override.yml up -d chat-island
```

An `image:` referencing `@sha256:<digest>` is honored by `docker compose up -d` and
resolves from local storage if present (no pull needed). Digest-pinning is exact and
immune to the tag being re-pushed under you.

**Simplest variant that fits the existing pipeline.** `update.sh` already keys the box's
running version off `ISLAND_VERSION` in `.env` (`image:
ghcr.io/…:${ISLAND_VERSION}`). You can make `ISLAND_VERSION` accept a **digest ref**:
set the base compose to
`image: ghcr.io/nickmeinhold/aiko-chat-island${ISLAND_REF}` where `ISLAND_REF` is either
`:edge` (normal) or `@sha256:<digest>` (rollback pin), and drive it from `.env`. Whether
you use an override file or an env-driven ref, the invariant is the same: **rollback pins
by index digest, not by tag.** The override-file approach is cleaner because it leaves
`.env` untouched and is trivially removed (`rm rollback.override.yml`) once a good
release lands.

### Concrete deploy/rollback sequence for `update.sh`

```bash
# BEFORE pulling — capture the current index digest as the rollback target.
LASTGOOD=$(docker inspect --format '{{index .RepoDigests 0}}' \
             "$(docker compose ps -q chat-island)" | sed 's/.*@//')
docker tag "ghcr.io/$REPO@${LASTGOOD}" ghcr.io/$REPO:lastgood   # pin ref so GC can't reap it (§4)

# ... existing backup-DB-first, then:
docker compose pull chat-island
docker compose up -d chat-island

# health gate
if ! curl -fsS --max-time 5 http://127.0.0.1:8095/health >/dev/null; then
    # ROLLBACK — local, no network; old image still present + tagged lastgood
    printf 'services:\n  chat-island:\n    image: ghcr.io/%s:lastgood\n' "$REPO" > rollback.override.yml
    docker compose -f compose.yaml -f rollback.override.yml up -d chat-island
    curl -fsS --max-time 5 http://127.0.0.1:8095/health >/dev/null \
      && notify "ROLLED BACK to $LASTGOOD" || notify "ROLLBACK FAILED — manual intervention"
    exit 1
fi
# success: record new state-of-truth
NEW=$(docker inspect --format '{{index .RepoDigests 0}}' "$(docker compose ps -q chat-island)" | sed 's/.*@//')
echo "$NEW" > /var/lib/aiko-island/deployed.digest
```

Note the health check should also confirm the DB is reachable / the container actually
came up, not just that the socket answers 200 (§6).

---

## 4. Prevent the rollback-target image from being GC'd between deploy and rollback

`docker image prune` (dangling only) and `docker image prune -a` / `docker system prune
-a` remove images with **no container using them and no tag/ref pointing at them**. The
moment you `compose up -d` the new image, the **old** image has no running container — so
an `-a` prune (or a cron janitor, or `compose up`'s own cleanup on some setups) can reap
your rollback target inside the deploy window. That is the single most likely way
auto-rollback silently breaks.

**Recommendation (do both):**

1. **Tag the last-known-good before pulling** (as in §3): `docker tag …@<LASTGOOD>
   ghcr.io/$REPO:lastgood`. A tagged image is not dangling and is not removed by `image
   prune` (without `-a`) and not by `system prune` unless it's `-a` **and** unused. The
   `:lastgood` tag is a durable, human-legible rollback handle.
2. **Do not prune inside the deploy/rollback window.** If a disk-cleanup job exists, it
   must run outside deploys and must exclude `:lastgood`. Only prune the *previous*
   lastgood **after** a new release has passed its health gate and you've re-pointed
   `:lastgood`.
3. Optionally protect via a container label / a keep-list, but a tag is the simplest
   durable pin and is what podman/watchtower-style tools effectively rely on.

**[uncertain]** whether the box runs any periodic prune today — the design must state
"no `prune -a` on a timer" as an explicit invariant, or the rollback guarantee is void.

---

## 5. Concurrency lock

Two collisions to prevent: (a) two watcher runs overlapping, (b) a watcher run colliding
with a human running `update.sh` by hand. **`flock` on a shared lock file covers both**;
systemd single-instance only covers (a).

### Is a `.timer`-triggered `.service` already single-instance?

**Yes, for its own overlap.** A systemd `.service` has one active instance per unit name.
If a timer fires while the previous activation is still running, systemd does **not** start
a second copy — the trigger is effectively coalesced (the unit is already `active`, and
`Type=oneshot` units in particular won't be re-run while running). So (a) is largely
handled by systemd *for the timer path*. But this does **not** protect against (b): a
human invoking `deploy/update.sh` directly shares nothing with the systemd unit's
instance lock.

**Therefore put the lock in `update.sh` itself** (the shared mutator — same "one door"
discipline as the codebase's backend gates), so every path — timer, manual, future
caller — passes the same gate:

```bash
# top of update.sh
exec 9>/run/aiko-island/update.lock          # fd 9 held for the life of the script
if ! flock -n 9; then
    echo "another deploy/update is in progress — aborting" >&2
    exit 69                                   # EX_UNAVAILABLE
fi
# lock auto-releases when fd 9 closes (process exit), even on crash/kill
```

- `flock -n` = non-blocking: **fail closed** immediately rather than queueing a second
  deploy behind the first. For a deploy you want abort-not-wait.
- **Lock file location:** `/run/aiko-island/` (tmpfs, cleared on reboot, correct for a
  runtime lock) is ideal; `/var/lock/aiko-island-update.lock` is the traditional
  alternative. Create the dir via systemd `RuntimeDirectory=aiko-island` (auto-created
  with right perms) or a `tmpfiles.d` entry. **Do not** put the lock inside the app data
  dir or the repo.
- **Host split:** on the `sudo -n docker` box, `update.sh` runs under the account that has
  docker access; ensure both the timer's `User=` and an interactive human resolve to a
  lock path both can write (a `/run/aiko-island` owned by that account, or a group). Flag
  for design: confirm the timer runs as the **same principal** that a human uses, else the
  lock protects nothing.
- Putting the lock in `update.sh` also means the systemd overlap protection becomes a
  belt-and-suspenders redundancy, not the sole mechanism — good.

---

## 6. Health signal trustworthiness (short)

A bare `200` from `/health` proves the process is **live** (socket bound, event loop
answering) — it does **not** prove **readiness** (DB attached, migrations at head, bus
client connected). A container can 200 on `/health` while the app is degraded, which
would let a broken deploy pass the gate and suppress the rollback.

Cheap upgrades, in order of value:

1. **Version/digest self-report:** have the app expose the image digest or a build id it
   was built with (env-injected at build) and assert it equals the digest you just
   deployed. Catches "pulled but old container still serving" and "wrong image entirely".
2. **Readiness, not just liveness:** make (or add) a `/health` that also does a trivial
   DB `SELECT 1` and reports bus-connectivity — so 200 means "actually serving", not
   "process exists".
3. **Post-deploy smoke of one real route** (e.g. an unauthenticated `/v1/...` that
   touches the DB) after the health 200, before declaring success.

Design should treat the current `/health` 200 as **necessary but not sufficient** and
adopt at least #1 (it's nearly free and directly closes the redeploy-loop / stale-container
failure mode). This mirrors podman auto-update's own lesson (§7): a naive "unit
started OK" signal is too weak; you need the app to affirm readiness.

---

## 7. Prior art — what to reuse vs avoid

### Watchtower (`containrrr/watchtower`) — **do not adopt as-is**
- **Detection:** exactly our model — polls the registry, compares the **local vs remote
  image digest** (`IsContainerStale`), and recreates the container when they differ.
  Confirms our digest-diff approach is the standard one.
- **Why unsuitable here:**
  - **No DB backup.** It stops-and-recreates with no pre-update hook for the SQLite
    `.backup()` our deploy requires. A bad migration would run with no restore point.
  - **No health-gated rollback.** "If the new image introduces breaking changes,
    Watchtower will still apply it." There is no built-in rollback; it can *remove* old
    images (`--cleanup`), which actively **destroys** the rollback target — the opposite
    of §4.
  - **No CI/approval integration**, no digest-pinned rollback path.
  - **[verified via docs] Watchtower was archived (read-only) by its owner in December
    2025; last release v1.7.1.** Building on an archived, unmaintained tool for the safety
    spine is a liability. **Avoid.**
- **Reuse:** only the *idea* (poll digests, recreate on change) — which we already have.

### Podman `auto-update` + systemd — **closest correct prior art; mirror its shape**
- **Detection:** `io.containers.autoupdate=registry` label; compares **local storage
  digest vs remote image digest**, pulls + restarts the systemd unit on mismatch. Same
  digest-diff model.
- **Rollback:** **enabled by default** (`--rollback=true`) — "if restarting a systemd
  unit after updating the image has failed, rollback to using the previous image and
  restart the unit another time." This is precisely our capture-old-digest →
  try-new → restore-old flow, and validates keeping the previous image around.
- **Critical caveat to steal:** podman's rollback trigger relies on **SDNOTIFY READY**,
  *not* a health check — "restarting the systemd unit may succeed even if the container
  has failed shortly after." Lesson for us: **"the unit started" is too weak a success
  signal — gate on an app-affirmed readiness probe** (our `/health` + §6), which is
  actually *stronger* than podman's default.

### "Watchtower + healthcheck rollback" community patterns
- These exist as **DIY scripts**, not a supported feature: capture current digest → pull
  → recreate → poll the container `HEALTH` status → if unhealthy, re-run with the saved
  digest. That is essentially the script we're writing. There is **no** turnkey,
  well-maintained "digest-diff + DB-backup + health-gated-rollback + notify" tool — which
  is the justification for building this bespoke and small. **[uncertain]** on any single
  canonical repo to cite; the pattern is folklore-level, so lean on podman's documented
  `--rollback` as the authoritative reference instead.

### Diun / dockcheck (mentioned in the ecosystem)
- **Diun** only *notifies* on new images (no update, no rollback) — could be a fallback
  notifier but does nothing we need. **dockcheck** is a bash update helper — same DIY
  tier. Neither adds DB-backup or health-gated rollback.

---

## 8. Notification

Cheapest robust path: `curl` to the **Telegram Bot API** `sendMessage` from bash.

```bash
notify() {  # $1 = message
    curl -fsS --max-time 10 \
      "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TG_CHAT_ID}" \
      --data-urlencode "text=[$(hostname)] island-deploy: $1" \
      -o /dev/null || logger -t island-deploy "notify failed: $1"
}
```

- Fire on **success**, **failure**, and **rollback** (success + failed-rollback are the
  two you must never miss). `notify` must **never** abort the deploy — swallow its errors
  (`|| logger …`) so a Telegram outage can't wedge a rollback.
- **Generic webhook** alternative (Slack/Discord/matrix/n8n): same shape, POST JSON to a
  webhook URL — keep `notify()` as the single indirection so the channel is swappable.

### Security note (the bot token is a secret on the box)
- The bot token grants full send rights for that bot — treat it like any credential.
- **Where it lives:** the box already carries secrets via `.env` (the compose
  `ISLAND_VERSION` / SOPS-age pattern). Put `TG_BOT_TOKEN` + `TG_CHAT_ID` in a
  **root/service-owned env file** (`/etc/aiko-island/notify.env`, mode `0600`, owned by
  the deploy principal), loaded by the systemd unit via `EnvironmentFile=` — **not** in
  the repo, **not** in the compose file, **not** on the `curl` command line of a
  long-running process (argv is world-readable via `ps`; a one-shot `curl` in a
  short-lived script is acceptable but prefer env-var substitution so the token isn't in
  the process table any longer than necessary).
- If SOPS is already the box's secret substrate, decrypt to the `0600` env file at deploy
  time rather than storing plaintext long-term. **[uncertain]** which secret mechanism
  each box actually uses today — the design should reuse whatever `.env`/SOPS pattern the
  islands already run, not introduce a new one.

---

## Open questions to hand the cage-match

1. **Is any periodic `docker … prune -a` running on either box?** If yes, the §4 rollback
   guarantee is void until it's fixed. Must be verified on the live boxes, not assumed.
2. **Does the timer run as the same principal a human uses for `update.sh`** (esp. the
   `sudo -n docker` box)? The §5 lock only works if both share a writable lock path.
3. **RepoDigests / `.Id` on the classic graphdriver store** — verified only on the
   containerd store here; if either live box uses the classic store, re-verify that
   `.RepoDigests[0]` carries the **index** digest there too (it should, since we pull by
   tag, but confirm on the actual box). The §2 state-file approach is the mitigation that
   makes this moot.
4. **buildx `unknown/unknown` attestation manifests** in the index — harmless for digest
   diffing (we compare the *index* digest, which already accounts for them) but worth a
   sentence so a reviewer doesn't mistake them for corruption.
5. **`docker buildx imagetools inspect --format` field stability** across versions if we
   ever prefer it over raw curl — pin/test on the box.
