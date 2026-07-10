# Passkeys (#1471) — deploy runbook

**Status as of 2026-06-30:** **Gate 1 CROSSED** — endpoints deployed DARK to prod
(alembic at `0008`, AASA serving via Caddy, `/providers` still hides passkey). Gate 2
(`passkey_enabled` flip) and gate 3 (device e2e) remain. This runbook records the
procedure that crossed gate 1 and the steps for 2 + 3.

Grounded against the live host on 2026-06-30 (all reads non-destructive). Re-verify
the "before" facts before running — a liveness claim has a shelf life.

---

## TL;DR

```
# from this repo on Nick's laptop, on main @ 45d4e5a (clean):
#   1. local gate:   run the suite
#   2. backup:       online-copy the sole-copy prod DB off-host FIRST
#   3. deploy dark:  rsync tree -> docker compose up -d --build   (MUST be --build)
#   4. verify dark:  0008 applied, AASA serves, /providers still hides passkey
#   5. device e2e:   register->authenticate on a real iPhone (gate 3; register
#                     creates the account directly, no claim — Design 04 Step 1)
#   6. flip on:      passkey_enabled=true, redeploy, confirm /providers advertises
```

The **one** command that matters: `docker compose up -d --build`. Without `--build`
the host recreates the container from the stale pre-passkey image and ships nothing
while reporting success.

---

## The three gates

1. **Not deployed.** Host code is stale (pre-passkey). Crossed by steps 1–4 below.
2. **`passkey_enabled=False`.** Advertisement off. Flip LAST (step 6), after device e2e.
3. **Device e2e unproven.** Needs a real iPhone + the app's `webcredentials`
   entitlement live (app PR#38). Not something the gateway side can self-verify.

Gates 1 and 2 are deliberately separate: deploying the endpoints **dark** lets the
flow be exercised on a real device (gate 3) before the feature is advertised to all
clients. Do not collapse them.

---

## Topology (grounded facts, re-verify before trusting)

- **Deploy source** = this repo on Nick's laptop, `main` @ `45d4e5a` (== passkey code).
- **Host** = `imagineering`, dir `~/apps/aiko-chat-gateway`. **Not a git repo** — a
  plain rsync'd tree. Deploy = rsync the tree, then rebuild on the host.
- **Image** is built ON THE HOST (`build: .`). No registry, no CI publisher
  (GHA is dead, won't-fix #18). `docker compose up -d` does **not** rebuild from
  changed source — pass **`--build`**.
- **Entrypoint** (`entrypoint.sh`) runs `python -m aiko_gateway.migrate` (fail-closed,
  stamp-or-upgrade) THEN uvicorn. A failed migration fails the container; uvicorn
  never serves an unmigrated schema.
- **Live schema** = alembic `0007`. `0008_passkey.py` adds `passkey_credentials` +
  `passkey_challenges` — lands as exactly one clean `upgrade` on top of `0007`
  (the adopt path does NOT trigger; the DB is already alembic-managed).
- **Caddy** = `chat.imagineering.cc { reverse_proxy localhost:8095 }` (in
  `~/apps/caddy/Caddyfile`). Clean catch-all, no `/.well-known/` interception →
  AASA/assetlinks pass straight through once the gateway serves them. **Not a gate.**
- **Config** needs NO changes for the dark deploy: `passkey_rp_id` defaults to
  `chat.imagineering.cc`, `passkey_ios_app_id` is baked, `passkey_enabled` defaults
  `False`, `JWT_SECRET` already lives in the host `.env`.
- **Compose project** = `aiko` (pinned via `name:` in `docker-compose.yml`, NOT derived
  from the host dir basename). Containers are `aiko-<service>-1`:
  `aiko-chat-island-1` (the gateway), `aiko-mosquitto-1`, `aiko-registrar-1`, `aiko-chat-1`.
- **DB volume** = `aiko_data`, declared **external** with a fixed name — decoupled from the
  project name, so future project/service renames never reproject (or silently empty) the
  store. Broker state (`mosquitto_data`) is a plain managed volume (transport-only, rebuilt
  on reconnect).
- **⚠ One-time project cutover** (only the FIRST deploy that moves prod from the old
  `aiko-chat-gateway` project to `aiko`): the live DB currently sits in the old managed volume
  `aiko-chat-gateway_aiko_gateway_data`. Because the project name changes, `--remove-orphans`
  will NOT reap the old containers (they belong to a *different* project). Do the cutover
  explicitly — see **"Project cutover"** below — BEFORE the normal deploy. After the cutover,
  steady-state deploys are plain `docker compose up -d --build`.

---

## Pre-flight: local gate (CI is dead — YOU are the gate)

```bash
cd /Users/nick/git/orgs/aiko/aiko-chat-island
git status -s                      # must be clean
git log --oneline -1               # must be 45d4e5a (passkey merge)
python -m pytest -q                # 348 tests must pass (incl. the real py_webauthn round-trip)
```

Do not proceed past a red suite. There is no CI backstop.

---

## Step 1 — Back up the sole-copy prod DB (FIRST, foreground)

The SQLite file is the ONLY copy of message history + auth + ACL. Online-safe hot
copy via Python's `sqlite3.Connection.backup()`, then pull it off-host. Mirrors the
existing `~/aiko-db-backups/aiko.db.predeploy-*` pattern.

> The container is `python:3.12-slim` — **no `sqlite3` CLI**. Use the stdlib
> `sqlite3` module's `.backup()` (a proper online hot backup), NOT a `sqlite3
> /data/aiko.db ".backup"` shell call (that errors `executable not found`).

```bash
TS=$(date +%Y%m%d-%H%M%S); C=aiko-chat-island-1
ssh imagineering "
set -e
docker exec $C python -c \"
import sqlite3
src=sqlite3.connect('/data/aiko.db'); dst=sqlite3.connect('/data/predeploy-$TS.db')
with dst: src.backup(dst)
print('integrity_check:', dst.execute('PRAGMA integrity_check').fetchone()[0],
      '| users:', dst.execute('select count(*) from users').fetchone()[0],
      '| channels:', dst.execute('select count(*) from channels').fetchone()[0],
      '| alembic:', dst.execute('select version_num from alembic_version').fetchone()[0])
\"
docker exec $C python -c \"import sqlite3,sys; sys.stdout.write('\n'.join(sqlite3.connect('/data/aiko.db').iterdump()))\" \
  > ~/aiko-db-backups/aiko.db.predeploy-$TS.sql
docker cp $C:/data/predeploy-$TS.db ~/aiko-db-backups/aiko.db.predeploy-$TS
docker exec $C rm /data/predeploy-$TS.db
ls -la ~/aiko-db-backups/aiko.db.predeploy-$TS*; tail -1 ~/aiko-db-backups/aiko.db.predeploy-$TS.sql"
```

Confirm `integrity_check: ok`, the expected row counts, the binary is non-trivial in
size, and the `.sql` dump ends with `COMMIT;`. A backup that didn't land = STOP
(restore correctness is the product, not a dump that ran).

---

## Project cutover — ONE TIME ONLY (old `aiko-chat-gateway` project → `aiko`)

Skip this entirely on steady-state deploys. Run it exactly once, the first deploy after
the compose project was pinned to `aiko` and the DB volume made external as `aiko_data`.
Do it AFTER the Step-1 backup, foreground, verifying each step before the next.

```bash
# 1. Create the external volume and copy the live DB into it from the OLD project volume.
#    (alpine one-shot: mount both, cp -a preserves everything. No container writing during copy.)
ssh imagineering '
set -e
docker volume create aiko_data
docker run --rm \
  -v aiko-chat-gateway_aiko_gateway_data:/from:ro \
  -v aiko_data:/to \
  alpine sh -c "cp -a /from/. /to/ && ls -la /to && echo COPIED"
'
# expect: aiko.db present in /to, non-trivial size, "COPIED".

# 2. Tear down the OLD project (its containers belong to project aiko-chat-gateway; the new
#    project can't see them, so --remove-orphans won't reap them). Volumes are NOT removed
#    by `down` without -v, so the old data volume stays as a fallback.
ssh imagineering 'cd ~/apps/aiko-chat-gateway && docker compose -p aiko-chat-gateway down'
```

Only after the copy is verified and the old project is down, proceed to Step 2. The old
volume `aiko-chat-gateway_aiko_gateway_data` is left intact as a rollback anchor — delete it
manually only once the new stack is proven healthy on `aiko_data`.

---

## Step 2 — Deploy dark (rsync tree, then rebuild)

```bash
cd /Users/nick/git/orgs/aiko/aiko-chat-island
# rsync the source tree to the host. NO --delete and EXCLUDE .env: the host .env is
# gitignored (absent locally) and holds JWT_SECRET + GITHUB_CLIENT_SECRET — --delete
# would erase it and crash the boot. The image is built from explicit Dockerfile
# COPY dirs, so leftover host files can't pollute it; --delete buys nothing here.
# DRY-RUN first (-avn) and eyeball the file list before the real run.
rsync -avn \
  --exclude '.git' --exclude '__pycache__' --exclude '.venv' --exclude '.pytest_cache' \
  --exclude 'node_modules' --exclude '.env' --exclude '*.bak*' --exclude '.claude' \
  ./ imagineering:~/apps/aiko-chat-gateway/          # add real run by dropping the n in -avn

# rebuild + recreate. --build is MANDATORY (no registry; image built from this tree).
ssh imagineering 'cd ~/apps/aiko-chat-gateway && docker compose up -d --build --remove-orphans'
```

Watch the entrypoint migrate before serving:

```bash
ssh imagineering "docker logs --since 2m aiko-chat-island-1 2>&1 | grep -A2 entrypoint"
# expect: "[entrypoint] migrating database to head..." then "[entrypoint] starting uvicorn..."
# NO "Refusing to adopt" / "Adopting" lines — the DB is already managed at 0007.
```

---

## Step 3 — Verify the dark deploy (each invariant, foreground)

```bash
C=aiko-chat-island-1
# (a) schema advanced 0007 -> 0008, passkey tables exist
ssh imagineering "docker exec $C python -c \"
import sqlite3; db=sqlite3.connect('/data/aiko.db')
print('alembic_version:', [r[0] for r in db.execute('select version_num from alembic_version')])
print('passkey tables:', [r[0] for r in db.execute(\\\"select name from sqlite_master where type='table' and name like 'passkey%'\\\")])\""
# expect: alembic_version: ['0008']   passkey tables: ['passkey_challenges','passkey_credentials']

# (b) .well-known now serves through Caddy (was 404 pre-deploy)
ssh imagineering "curl -s https://chat.imagineering.cc/.well-known/apple-app-site-association"
# expect: {"webcredentials":{"apps":["SPL85G447K.cc.imagineering.aikoChatApp"]}}

# (c) passkey is STILL DARK — /providers must NOT list it yet
ssh imagineering "curl -s https://chat.imagineering.cc/v1/auth/providers"
# expect apple/google/github only — NO {"slug":"passkey"}

# (d) health
ssh imagineering "curl -s https://chat.imagineering.cc/health"   # {"status":"ok",...}
```

If (c) shows passkey, the flag leaked on — investigate before going further.
At this point gate 1 is crossed; gates 2 + 3 remain.

---

## Step 4 — Device e2e (gate 3, needs a real iPhone)

Requires app PR#38 live with the `webcredentials:chat.imagineering.cc` entitlement.
On the device, drive the full ceremony against the dark endpoints:
`passkey/register/start` -> `finish` -> `passkey/authenticate/start` -> `finish`.
As of Design 04 Step 1 (#1728), `register/finish` creates the account directly
(auto-generated handle) and returns session tokens — there is NO `/social/claim`
step for passkeys anymore. Confirm a `passkey_credentials` row is created at
`register/finish` (not at claim) and that authenticate returns a session for the
same user.

Tail the gateway while testing:
```bash
ssh imagineering "docker logs -f aiko-chat-island-1"
```

**Android is blocked** until app task #20 supplies the Play App Signing SHA-256
(`passkey_android_cert_sha256` + the apk-key-hash in `passkey_extra_origins`).
assetlinks serves `[]` until then by design. iOS is unaffected.

---

## Step 5 — Flip the advertisement on (gate 2, LAST)

Only after device e2e passes. Consider landing follow-up **#28** (rate-limit +
request-size on the ungated passkey ceremonies) FIRST — the endpoints are public and
unauthenticated; a flag flip is the moment they start getting real traffic.

```bash
# add to the host env (compose reads ${...} from ~/apps/aiko-chat-gateway/.env)
ssh imagineering "grep -q '^PASSKEY_ENABLED=' ~/apps/aiko-chat-gateway/.env \
  || echo 'PASSKEY_ENABLED=true' >> ~/apps/aiko-chat-gateway/.env"
```
> NOTE: `passkey_enabled` is a plain pydantic-settings bool — confirm the compose
> file passes `PASSKEY_ENABLED` into the container env (it does NOT today; the
> dark-deploy needs no env, so this var isn't wired yet). **Before flipping, add**
> `PASSKEY_ENABLED: ${PASSKEY_ENABLED:-false}` to the `environment:` block in
> `docker-compose.yml` (commit it to the repo first, then rsync), OR flip the
> default in `config.py`. Don't hand-edit only the host copy — that's config drift.

Then redeploy and confirm advertisement:
```bash
ssh imagineering "cd ~/apps/aiko-chat-gateway && docker compose up -d --build --remove-orphans"
ssh imagineering "curl -s https://chat.imagineering.cc/v1/auth/providers"
# expect {"slug":"passkey","display_name":"Passkey","kind":"passkey"} now present
```

Passkeys are **live** only after step 5's `/providers` confirms the advertisement
AND a real device has completed the round-trip (step 4). Until both, name the open
gate — don't say "done".

---

## Rollback

The deploy is reversible at the image layer (recreate from the prior image) and the
data layer (the step-1 backup). Migration `0008` is additive (two new tables, no
column drops), so a forward deploy can't corrupt existing rows; but if you must
revert the schema:

```bash
# revert code: rsync the prior commit's tree and rebuild, OR re-tag the old image.
# revert schema (only if needed): downgrade one revision
ssh imagineering "docker exec aiko-chat-island-1 \
  python -c \"from alembic.config import Config; from alembic import command; \
  c=Config('alembic.ini'); c.set_main_option('script_location','alembic'); \
  command.downgrade(c,'0007')\""
# worst case: restore the step-1 backup (see the restore drill, #17).
```
