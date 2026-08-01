#!/bin/sh
# Container entrypoint: migrate the DB to head, THEN serve.
#
# `set -e` makes a failed migration fail the container (fail-closed): uvicorn is
# only reached if `aiko_gateway.migrate` exits 0, so the app never serves an
# unmigrated schema. There is no host orchestrator to sequence migrate-before-boot
# (deploy is a manual `docker compose up -d` — aiko-chat-island#19), so the
# ordering must live here.
set -e

echo "[entrypoint] migrating database to head..."
python -m aiko_gateway.migrate

echo "[entrypoint] starting uvicorn..."
# SINGLE WORKER ON PURPOSE — do NOT add `--workers N` (or scale to multiple
# replicas) until the #46 cross-worker session-reconciliation sweep lands. The
# realtime Hub keeps WS connections in per-process memory, so an active-disconnect
# (ban / recovery re-key / account deletion) only reaches sockets on the worker
# that handled it — a banned or deleted user's live socket on another worker would
# ride on until natural close. The tempered design for the cross-worker fix is
# docs/crucible/46-cross-worker-disconnect/CAST.md; build it before going wide.
#
# This is now ENFORCED, not just documented: aiko_gateway.worker_guard makes each
# booting process contend for an exclusive flock on the data volume, so a second
# worker/replica FAILS to boot (see #46 guard). When #46 lands and you add
# `--workers N` here, also `export GATEWAY_ALLOW_MULTIWORKER=true` on the line above
# this exec to lift the guard.
exec uvicorn aiko_gateway.main:app --host 0.0.0.0 --port 8095
