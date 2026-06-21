#!/usr/bin/env bash
#
# Phase 0 spike orchestrator. Stands up a LOCAL aiko stack and runs spike.py
# to answer the echo-semantics GATE (plan §Phase 0). Tears everything down on
# exit. Safe to re-run; uses a scratch work dir and a private broker on :1883.
#
#   ./run_spike.sh
#
set -uo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GW_DIR="$(dirname "$SPIKE_DIR")"
VENV_BIN="$GW_DIR/.venv/bin"
WORK="$SPIKE_DIR/work"
LOGS="$SPIKE_DIR/logs"
MOSQUITTO_BIN="/opt/homebrew/sbin/mosquitto"

rm -rf "$WORK" "$LOGS"
mkdir -p "$WORK" "$LOGS"

export PATH="$VENV_BIN:$PATH"
export AIKO_MQTT_HOST="localhost"
# Isolated port: a stale aiko stack may already hold 1883 (it did, from Jun 8).
export AIKO_MQTT_PORT="1884"
export AIKO_NAMESPACE="aiko"
export SPIKE_NONCE="nonce-$$-${RANDOM}"

PIDS=()
cleanup() {
  echo "[run_spike] tearing down (pids: ${PIDS[*]:-none})"
  # Kill only OUR processes by PID. Do NOT use `aiko_chat exit` — it blocks on
  # discovery and wedges the script. SIGTERM then SIGKILL the tree we started.
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
  sleep 1
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null
  done
}
trap cleanup EXIT INT TERM

wait_for_port() {  # host port timeout_s
  local host="$1" port="$2" timeout="$3" t=0
  while ! nc -z "$host" "$port" 2>/dev/null; do
    sleep 0.3; t=$((t+1))
    if [ "$t" -gt $((timeout*3)) ]; then echo "[run_spike] timeout waiting for $host:$port"; return 1; fi
  done
}

# Fail fast if our isolated port is already taken — otherwise we'd silently
# latch onto a foreign broker and tangle registrars (the Jun-8 collision).
if nc -z localhost 1884 2>/dev/null; then
  echo "[run_spike] FATAL: port 1884 already in use. Something else holds it; aborting."
  exit 1
fi
echo "[run_spike] 1/4 starting mosquitto on :1884 (+ws :9002)"
"$MOSQUITTO_BIN" -c "$SPIKE_DIR/mosquitto.conf" >"$LOGS/mosquitto.log" 2>&1 &
PIDS+=($!)
wait_for_port localhost 1884 10 || { echo "[run_spike] broker failed to bind — see $LOGS/mosquitto.log"; exit 1; }
echo "[run_spike]     broker up on :1884"

echo "[run_spike] 2/4 starting aiko_registrar"
"$VENV_BIN/aiko_registrar" >"$LOGS/registrar.log" 2>&1 &
PIDS+=($!)
sleep 2

echo "[run_spike] 3/4 bootstrapping HyperSpace + starting ChatServer"
# Replicate chat_start.sh's bootstrap in our scratch work dir.
cd "$WORK"
CHAT_SERVER_PATH="_chat_server_"
mkdir -p "$CHAT_SERVER_PATH"; cd "$CHAT_SERVER_PATH"
"$VENV_BIN/aiko_storage_file" initialize                              >"$LOGS/bootstrap.log" 2>&1
"$VENV_BIN/aiko_storage_file" create --bootstrap channels            >>"$LOGS/bootstrap.log" 2>&1
for ch in general random llm robot yolo; do
  "$VENV_BIN/aiko_storage_file" add --bootstrap "channels/$ch"       >>"$LOGS/bootstrap.log" 2>&1
done
"$VENV_BIN/aiko_storage_file" create --bootstrap users               >>"$LOGS/bootstrap.log" 2>&1
"$VENV_BIN/aiko_chat" run                                             >"$LOGS/chatserver.log" 2>&1 &
PIDS+=($!)
echo "[run_spike]     waiting for ChatServer to register..."
sleep 5

echo "[run_spike] 4/4 running spike.py (nonce=$SPIKE_NONCE)"
cd "$SPIKE_DIR"
# tee to a log that survives (logs/ was recreated above) — the parent's stdout
# redirect points at a since-deleted inode, so capture here too.
set -o pipefail
"$VENV_BIN/python" spike.py 2>&1 | tee "$LOGS/spike.log"
RESULT=${PIPESTATUS[0]}

echo "[run_spike] spike exit code: $RESULT"
echo "[run_spike] logs in $LOGS/ (mosquitto, registrar, bootstrap, chatserver)"
exit $RESULT
