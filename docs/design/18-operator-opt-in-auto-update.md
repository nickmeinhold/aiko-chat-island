# Design 18: Operator opt-in auto-update (channel, restore-on-failure, compat guard)

**Status:** CANDIDATE, for one `/design-temper` round. Nothing is built.
**Answers:** Nick, 2026-08-16 and again 2026-10-10: *"each island operator should have
the option to have the server automatically pull the latest image… it's what I want."*
**Supersedes** the parked shape in `docs/crucible/reactive-deploy/` (#236), whose temper
was FATAL on one flaw. This design exists to answer that flaw without the migration
lint (#248) it was parked behind.

## The flaw this must answer

Rolling back the **image** does not roll back the **database**. v2 migrates the volume
from schema N to N+1 on boot, fails `/health`, and v1 restarted on N+1 either
crash-loops or corrupts data (reactive-deploy TEMPER, 3/3 families). And today
`migrate.py`'s forward-revision tolerance makes the second outcome the default: an image
that meets an unknown revision **serves anyway**, and its own log line admits this is
*"Safe ONLY if the schema is backward- (N-1) compatible… it is NOT yet enforced."*

## Prior art (each piece is borrowed, not invented)

- **Channels:** Fedora CoreOS update streams + Zincati; snap channels. The box follows a
  moving tag, and a small agent applies what arrives.
- **Restore data on failed update:** snapd snapshots revision-specific data on refresh
  and restores it on revert.
- **Compat guard:** Synapse stores the oldest code version that can read the database
  (`schema_compat_version`) and **refuses to start** when the code is older.

## The design (three parts, each useful alone)

### 1. Compat guard in `migrate.py`, built first and needed regardless

- Each alembic revision may declare `compat_floor = "<revision id>"`: the oldest
  revision whose CODE can safely run against this schema. If it declares none, the floor
  is the revision itself, i.e. not backward-compatible. **Safe by default:** an author who
  forgets gets a refusal, never a corruption.
- `migrate.py` records the effective floor (the max over applied revisions) in a
  one-row table `schema_compat`.
- On boot, the unknown-revision branch changes from *serve anyway* to: **serve only if
  `schema_compat.floor` is a revision present in this image's script directory;
  otherwise exit non-zero** with `MIGRATE_REFUSE_TOO_OLD`. "Present in my scripts" is
  exactly "I am new enough". Alembic's graph does the version comparison, with no
  version arithmetic.
- Effect: the FATAL scenario becomes a loud refusal to start. This also protects a
  *manual* rollback, which today can corrupt silently.
- **A declared floor can be wrong.** CI checks it: for each release, boot the previous
  release's image against the new head's schema and run its smoke suite. That's the
  runtime-compat test from task #11, which finally has a precise job: verifying a
  declaration, not inferring safety.

### 2. Channel

- `release.yml` already publishes `:latest`, which tracks semver releases, never `main`.
  **That is the channel**; nothing new is needed in CI for v1.
- The operator's setting: `AUTO_UPDATE=off` (the **default**) or `AUTO_UPDATE=latest`.
  With `off` the island behaves exactly as today, and `update_nudge` still tells the
  operator when they're behind.
- **Stagger:** `AUTO_UPDATE_DELAY_HOURS` (default 24) means a release is applied only
  once it has been on the channel that long. Nick's two islands get different delays
  (imagineering 0, enspyr 24), so a bad release hits one island, not both. This answers
  the TEMPER's "simultaneous dual-prod = global outage". Third-party operators inherit
  the 24 h soak behind everyone with a shorter one.

### 3. The watcher: a systemd timer on the box, shell only (ISL-0003 holds)

- Every 15 min it resolves the channel's **index digest** (anonymous; GHCR is public)
  and compares it with the running image's index digest, so there's no per-arch loop.
- On a new digest older than the delay, it runs `update.sh --auto`. **`update.sh` takes
  no lock today** (checked 2026-10-10; an older memory said it did). So this design ADDS
  one: `flock -n` on a file in the deploy dir, taken by every `update.sh` run, manual
  or timer. A second run exits "deploy already in progress" rather than queueing, and
  the timer runs as the same user that runs manual deploys, or the lock is decorative.
  The run:
  1. **pin the current digest** as a local tag `aiko-island:previous` (registry GC and
     `prune` can't reap it; nothing on a timer ever prunes);
  2. **stop** gateway, registrar and chat, so no process can write;
  3. back up `aiko.db` by **file copy** (consistent, because nothing is running) and
     run `integrity_check`;
  4. pull the new digest, `up -d`, and wait for `/health`;
  5. **healthy:** record the digest as current. Done.
  6. **never healthy within 120 s:** stop, **restore `aiko.db` from step 3**, start
     `aiko-island:previous`, verify `/health`, record the digest as **quarantined**, and
     notify the operator.
- **Why the restore loses nothing:** the new version never served (it never passed
  `/health`), and the old one was stopped before the backup. Between steps 2 and 6 no
  process accepted a write. The cost is downtime of about the pull time plus 120 s
  worst case, which an auto-updating operator accepts.
- **Quarantine:** a quarantined digest is never retried *automatically*, but the next
  release supersedes it. A transient fault (pull error, GHCR 5xx, no disk) **aborts
  before step 2** and simply retries next poll, so it is never quarantined. Only a
  release that *started and failed health* is.

## Deliberately out of scope

- **Healthy-but-wrong releases** (pass `/health`, broken in use). Auto-rollback can't
  see them; nothing unwatched can. The stagger bounds their blast radius to one island,
  and the compat guard makes the operator's manual rollback safe.
- **Push-CD** (CI holding SSH keys to prod boxes): rejected 2026-08-16; still rejected.
  The island pulls; nobody pushes into it.
- **Config/compose delivery** (#2301): a release that also needs a compose change is
  refused by `preflight-compose-drift.sh` before step 2. That's a clean abort, and the
  operator syncs by hand as today.

## Open questions for the temper

1. **Bus traffic during the window.** Gateway, registrar and chat stop together. Does
   anything published to MQTT while they are down need to survive, or is that already
   the behaviour of every manual deploy today?
2. **Is 120 s of `/health` enough evidence of "never served"?** `/health` passing is
   liveness. Could a version accept writes *before* it first passes `/health`? (The
   entrypoint migrates before uvicorn starts, so a write needs uvicorn, but check.)
3. **Should `compat_floor` default to "not compatible"** (refuse, the safe default) given
   that most of the last six migrations were CHECK-adding table rebuilds that old code
   would very likely survive?
