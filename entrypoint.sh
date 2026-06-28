#!/bin/sh
# Container entrypoint: migrate the DB to head, THEN serve.
#
# `set -e` makes a failed migration fail the container (fail-closed): uvicorn is
# only reached if `aiko_gateway.migrate` exits 0, so the app never serves an
# unmigrated schema. There is no host orchestrator to sequence migrate-before-boot
# (deploy is a manual `docker compose up -d` — aiko_chat_gateway#19), so the
# ordering must live here.
set -e

echo "[entrypoint] migrating database to head..."
python -m aiko_gateway.migrate

echo "[entrypoint] starting uvicorn..."
exec uvicorn aiko_gateway.main:app --host 0.0.0.0 --port 8095
