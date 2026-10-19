#!/usr/bin/env bash
# Design 18 v4 evidence: Phase B's failure arm, exercised against the REAL published
# images on a throwaway volume. Each step asserts its own outcome; any surprise exits 1.
#
#   E1  stopped-stack backup: a one-shot container with CMD overridden backs up the db
#       WITHOUT migrating it (entrypoint.sh is CMD, not ENTRYPOINT)
#   E2  admission (positive control): the new image migrates and verify_schema admits HEAD
#   E3  refusal: the previous image refuses the forward-migrated db (Phase B's
#       "marker present => previous refused => terminal" arm is reachable)
#   E4  refusal, new guard: the new image refuses a stamp it has never seen
#   E5  recovery exercised from the state that triggers it: restore the set, previous admitted
#
# ENVIRONMENT=development only skips config.py's prod boot-secret guards (JWT, signing
# seed). migrate.py and db.py never read `environment`, so the guard under test runs
# exactly as in production (checked: git grep environment -- migrate.py db.py = 0).
#
# Shell, not Dart: it only orchestrates `docker run`, and the watcher it evidences is
# itself box-side shell (ISL-0003).
#
# Usage: phase-b-evidence.sh <old-tag> <new-tag>     e.g. 0.16.1 0.17.0
set -euo pipefail
OLD="ghcr.io/nickmeinhold/aiko-chat-island:${1:?old tag}"
NEW="ghcr.io/nickmeinhold/aiko-chat-island:${2:?new tag}"
V="d18ev-$$"; V2="d18ev2-$$"; BK="$(mktemp -d)"
JWT="$(openssl rand -hex 32)"
cleanup() { docker volume rm -f "$V" "$V2" >/dev/null 2>&1 || true; rm -rf "$BK"; }
trap cleanup EXIT

run() { local img="$1" vol="$2"; shift 2
  docker run --rm -e ENVIRONMENT=development -e JWT_SECRET="$JWT" \
    -e DB_URL=sqlite+aiosqlite:////data/aiko.db \
    -v "$vol:/data" -v "$BK:/bk" "$img" "$@"; }
MIGRATE=(python -m aiko_gateway.migrate)
VERIFY=(python -c 'import asyncio; from aiko_gateway.db import verify_schema; asyncio.run(verify_schema()); print("ADMITTED")')
STAMP=(python -c 'import sqlite3; print("STAMP", [r[0] for r in sqlite3.connect("file:/data/aiko.db?mode=ro", uri=True).execute("select version_num from alembic_version")])')
BACKUP=(python -c '
import sqlite3, sys
src = sqlite3.connect("/data/aiko.db"); dst = sqlite3.connect("/bk/pre.db")
with dst: src.backup(dst)
r = dst.execute("PRAGMA integrity_check").fetchone()[0]; print("integrity_check:", r); sys.exit(r != "ok")')
RESTORE=(sh -c 'rm -f /data/aiko.db /data/aiko.db-wal /data/aiko.db-shm && cp /bk/pre.db /data/aiko.db && echo RESTORED')
FUTURE=(python -c '
import shutil, sqlite3
shutil.copy("/bk/fwd.db", "/data/aiko.db")
c = sqlite3.connect("/data/aiko.db")
c.execute("update alembic_version set version_num = ?", ("0099_from_the_future",)); c.commit()')

migrate_stamp()  { run "$1" "$2" "${MIGRATE[@]}" >/dev/null && run "$1" "$2" "${STAMP[@]}"; }
migrate_verify() { run "$1" "$2" "${MIGRATE[@]}" && run "$1" "$2" "${VERIFY[@]}"; }

expect() { # expect <label> <want-exit:0|nonzero> <must-grep> -- cmd...
  local label="$1" want="$2" pat="$3"; shift 4
  local out rc=0; out="$("$@" 2>&1)" || rc=$?
  if { [ "$want" = 0 ] && [ $rc -ne 0 ]; } || { [ "$want" != 0 ] && [ $rc -eq 0 ]; }; then
    printf 'FAIL %s: exit %s (wanted %s)\n%s\n' "$label" "$rc" "$want" "$out"; exit 1; fi
  if ! grep -qF -- "$pat" <<<"$out"; then
    printf 'FAIL %s: output lacks %s\n%s\n' "$label" "$pat" "$out"; exit 1; fi
  printf 'ok   %-50s exit=%-3s %s\n' "$label" "$rc" "$(grep -m1 -oF -- "$pat" <<<"$out")"
}

echo "old=$OLD  new=$NEW"
expect "setup: old image creates the db"                0 "STAMP"    -- migrate_stamp "$OLD" "$V"
OLDHEAD="$(run "$OLD" "$V" "${STAMP[@]}")"; echo "     old head: ${OLDHEAD#STAMP }"
expect "E1 stopped-stack backup, one-shot NEW image"    0 "integrity_check: ok" -- run "$NEW" "$V" "${BACKUP[@]}"
expect "E1 ...did NOT migrate (stamp unchanged)"        0 "$OLDHEAD" -- run "$NEW" "$V" "${STAMP[@]}"
expect "E2 new image migrates forward"                  0 "STAMP"    -- migrate_stamp "$NEW" "$V"
echo "     new head: $(run "$NEW" "$V" "${STAMP[@]}" | sed 's/^STAMP //')"
expect "E2 positive control: new image ADMITS its head" 0 "ADMITTED" -- run "$NEW" "$V" "${VERIFY[@]}"
expect "E3 previous image REFUSES the forward db"       1 "Refusing" -- migrate_verify "$OLD" "$V"
# E4 on a copy, so E5 still starts from E3's state.
run "$NEW" "$V" cp /data/aiko.db /bk/fwd.db
run "$NEW" "$V2" "${FUTURE[@]}"
expect "E4 new guard REFUSES an unknown stamp"          1 "MIGRATE_REFUSE_UNKNOWN_REVISION" -- run "$NEW" "$V2" "${MIGRATE[@]}"
expect "E5 recovery: restore the set (db, -wal, -shm)"  0 "RESTORED" -- run "$NEW" "$V" "${RESTORE[@]}"
expect "E5 ...previous image ADMITTED after restore"    0 "ADMITTED" -- migrate_verify "$OLD" "$V"
echo "ALL EVIDENCE HELD"
