#!/usr/bin/env bash
#
# Reusable LOCAL aiko stack for gateway dev (broker + registrar + ChatServer),
# left RUNNING in the background (unlike run_spike.sh which tears down).
#
#   ./devstack.sh up     # start broker(:1884)+registrar+chatserver, leave up
#   ./devstack.sh down   # stop them (by recorded PIDs)
#   ./devstack.sh status
#
set -uo pipefail
SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GW_DIR="$(dirname "$SPIKE_DIR")"
VENV_BIN="$GW_DIR/.venv/bin"
WORK="$SPIKE_DIR/devwork"
LOGS="$SPIKE_DIR/devlogs"
PIDFILE="$WORK/devstack.pids"
MOSQUITTO_BIN="/opt/homebrew/sbin/mosquitto"

export PATH="$VENV_BIN:$PATH"
export AIKO_MQTT_HOST="localhost" AIKO_MQTT_PORT="1884" AIKO_NAMESPACE="aiko"

up() {
  if [ -f "$PIDFILE" ] && kill -0 "$(head -1 "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
    echo "[devstack] already up (pidfile $PIDFILE). Run 'down' first."; exit 1
  fi
  if nc -z localhost 1884 2>/dev/null; then
    echo "[devstack] FATAL: :1884 already in use; aborting."; exit 1
  fi
  rm -rf "$WORK" "$LOGS"; mkdir -p "$WORK" "$LOGS"; : > "$PIDFILE"

  echo "[devstack] mosquitto :1884"
  "$MOSQUITTO_BIN" -c "$SPIKE_DIR/mosquitto.conf" >"$LOGS/mosquitto.log" 2>&1 &
  echo $! >> "$PIDFILE"
  until nc -z localhost 1884 2>/dev/null; do sleep 0.3; done

  echo "[devstack] aiko_registrar"
  "$VENV_BIN/aiko_registrar" >"$LOGS/registrar.log" 2>&1 &
  echo $! >> "$PIDFILE"
  sleep 2

  echo "[devstack] bootstrap HyperSpace + aiko_chat run"
  cd "$WORK"; mkdir -p _chat_server_; cd _chat_server_
  "$VENV_BIN/aiko_storage_file" initialize             >"$LOGS/bootstrap.log" 2>&1
  "$VENV_BIN/aiko_storage_file" create --bootstrap channels >>"$LOGS/bootstrap.log" 2>&1
  for ch in general random llm robot yolo; do
    "$VENV_BIN/aiko_storage_file" add --bootstrap "channels/$ch" >>"$LOGS/bootstrap.log" 2>&1
  done
  "$VENV_BIN/aiko_storage_file" create --bootstrap users >>"$LOGS/bootstrap.log" 2>&1
  "$VENV_BIN/aiko_chat" run >"$LOGS/chatserver.log" 2>&1 &
  echo $! >> "$PIDFILE"
  echo "[devstack] up. pids: $(tr '\n' ' ' < "$PIDFILE"). logs: $LOGS/"
}

down() {
  if [ ! -f "$PIDFILE" ]; then echo "[devstack] no pidfile; nothing to stop."; return 0; fi
  while read -r pid; do [ -n "$pid" ] && kill "$pid" 2>/dev/null; done < "$PIDFILE"
  sleep 1
  while read -r pid; do [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null; done < "$PIDFILE"
  rm -f "$PIDFILE"; echo "[devstack] down."
}

status() {
  [ -f "$PIDFILE" ] || { echo "[devstack] down (no pidfile)"; return 0; }
  while read -r pid; do
    [ -n "$pid" ] && { kill -0 "$pid" 2>/dev/null && echo "  pid $pid UP" || echo "  pid $pid DEAD"; }
  done < "$PIDFILE"
}

case "${1:-}" in
  up) up ;; down) down ;; status) status ;;
  *) echo "usage: $0 {up|down|status}"; exit 2 ;;
esac
