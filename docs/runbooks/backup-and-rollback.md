# Backup and rollback

The island's SQLite store (message history, auth, ACLs, communities) is the **only
copy** of that data. It lives in the `/data` volume of the `chat-island` container.
Deploys are pull-based ([ISL-0003](../adr/ISL-0003-pull-based-deploy-of-pinned-image.md)):
set `ISLAND_IMAGE` in `image.env`, run `deploy/update.sh`.

## Backup

**Every `update.sh` run backs up first, fail-closed.** Step 1 takes an online hot copy
with Python's `sqlite3.Connection.backup()` inside the container (the slim image has no
`sqlite3` CLI), gates on `PRAGMA integrity_check`, copies it out to
`backups/aiko.db.preupdate-<ts>` in the deploy dir, and aborts before touching the
stack if any of that fails. `--no-backup` skips it; then a current backup is yours to
take.

For a backup outside a deploy (before real users arrive, or on a schedule), run the
same commands as `update.sh`'s step 1 block (`# --- step 1: back up the sole-copy DB`).
That block is the maintained recipe; this page does not copy it, so the two cannot
drift. A backup has landed when `integrity_check` is `ok` and the file is non-trivial
in size. A backup that didn't land means STOP.

## Rollback

The deploy is reversible at the image layer (recreate from the prior image) and the
data layer (the pre-update backup). **An image older than the database's schema
refuses to start** (`MIGRATE_REFUSE_UNKNOWN_REVISION`, design 18 §1; before #392 it
crash-looped in `verify_schema()` with advice that could not work). So:

- **The release ran no migration:** re-pin `image.env` to the prior release and
  run `update.sh`. That's all.
- **Rolling back BELOW v0.17.2** (the release that moved the pin to `image.env`,
  claude-tasks#5165): **keep this box's current `docker-compose.yml` and `deploy/`, and
  pass `--skip-drift-check`.** The drift guard will refuse, because the older tag's
  compose differs, and its advice to "sync the named files from the tag" is WRONG
  here. That older compose reads `${ISLAND_VERSION:-edge}` from `.env`, and once the
  SOPS re-lift has removed `ISLAND_VERSION`, it resolves `edge`: the island moves to
  `main` with a clean-looking deploy. The current compose runs older images unchanged
  (only the image lines differ), so keeping it is safe.
- **The release ran a migration:** put the database back FIRST, with the stack DOWN
  (`deploy/compose.sh down`: a merely stopped `restart: always` container comes back if
  the Docker daemon restarts mid-restore), then go back to the old image. **Restore
  that deploy's backup** (`backups/aiko.db.preupdate-<ts>`; see the restore drill,
  #17). It is exact, but it loses writes made since the update.

There is deliberately no serve-anyway override and no generic downgrade recipe here.
A downgrade has to run from the NEWER image (only it has the downgrade scripts), with
the stack stopped (never `docker exec` alembic DDL beside a live writer), and to the
OLD image's own head, which is a different revision on every release. Alembic
downgrades of table rebuilds are not guaranteed lossless. Restore the backup.

Backups from the June 2026 build-on-host era live at
`~/aiko-db-backups/aiko.db.predeploy-*` instead; use the backup the deploy you're
undoing actually took.
