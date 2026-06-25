"""SPIKE (#1281 incr 2, Decision B validation gate): does a ChatServer RESTART
emit spurious EC `remove` events that would trigger a hard-delete?

Decision B of docs/design/01-channel-topology-reconcile.html says the gateway
hard-deletes a channel + its history ONLY on a live-producer EC `remove`, and
NEVER on ChatServer disconnect — on the claim that a producer restart re-issues
`add`s rather than clear-then-remove. That claim is load-bearing for an
IRREVERSIBLE delete, so it must be RUN, not reasoned (concept_verify_by_running).

This probe behaves exactly like the gateway will:
  * discovers ChatServer; on each `_discovery_add` attaches a fresh
    ECConsumer(filter="channel_list") and registers a reconcile observer;
  * on `_discovery_remove` (ChatServer gone) tears the consumer down and does
    NOT signal any DB-remove;
  * the reconcile observer logs EVERY share command (add/remove/update) with a
    timestamp and a monotonic phase label.

Sequence: bring the stack up (devstack), observe the initial 5 adds, then KILL
and RELAUNCH the ChatServer while the consumer stays alive, and watch what the
reconcile observer sees across the restart.

PASS  (Decision B holds): zero `remove` events reach the reconcile observer; the
      restart manifests as a discovery_remove -> discovery_add -> re-`add` storm.
FAIL  (Decision B escalates): one or more `remove` events fire — a transient
      restart would have deleted history. Guard must become debounce/N-stable.

Run from the gateway repo root with the venv python. Manages the stack itself.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("AIKO_MQTT_HOST", "localhost")
os.environ.setdefault("AIKO_MQTT_PORT", "1884")
os.environ.setdefault("AIKO_NAMESPACE", "aiko")

import aiko_services as aiko  # noqa: E402
from aiko_chat.chat import ChatServer, get_server_service_filter  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SPIKE = REPO / "spike"
DEVSTACK = SPIKE / "devstack.sh"
WORK = SPIKE / "devwork"
CHAT_WORKDIR = WORK / "_chat_server_"
AIKO_CHAT_BIN = REPO / ".venv" / "bin" / "aiko_chat"
LOGS = SPIKE / "devlogs"

_t0 = time.time()
_phase = {"label": "startup"}
_events: list = []        # (t, phase, command, item_name)
_removes: list = []       # the dangerous ones
_discovery: list = []     # (t, phase, "add"|"remove", topic_path)


def _log(msg: str) -> None:
    print(f"[{time.time()-_t0:6.2f}s][{_phase['label']:9}] {msg}", flush=True)


class ProbeActor(aiko.Actor):
    def __init__(self, context):
        context.call_init(self, "Actor", context)
        self.ec_consumer = None
        self.cache: dict = {}
        aiko.do_discovery(
            ChatServer, get_server_service_filter(),
            self._discovery_add, self._discovery_remove,
        )

    def _discovery_add(self, service_details, service):
        topic_path = service_details[0]
        _discovery.append((time.time() - _t0, _phase["label"], "add", topic_path))
        _log(f"discovery_add  {topic_path}")
        # Gateway behaviour: fresh consumer per producer instance.
        self.cache = {}
        self.ec_consumer = aiko.ECConsumer(
            self, 0, self.cache, f"{topic_path}/control", filter="channel_list")
        self.ec_consumer.add_handler(self._reconcile_observer)

    def _discovery_remove(self, service_details):
        topic_path = service_details[0] if service_details else "?"
        _discovery.append((time.time() - _t0, _phase["label"], "remove", topic_path))
        _log(f"discovery_remove {topic_path}  (NO db-remove signalled — by design)")
        if self.ec_consumer:
            try:
                self.ec_consumer.terminate()
            except Exception:
                pass
            self.ec_consumer = None

    def _reconcile_observer(self, consumer_id, command, item_name, item_value):
        _events.append((time.time() - _t0, _phase["label"], command, item_name))
        if command == "remove":
            _removes.append((time.time() - _t0, _phase["label"], item_name))
            _log(f"** RECONCILE REMOVE ** {item_name}   <-- would hard-delete!")
        else:
            _log(f"reconcile {command} {item_name}")


def _run_aiko():
    init_args = aiko.actor_args(
        "probe_restart", protocol=f"{aiko.SERVICE_PROTOCOL_AIKO}/probe:0",
        tags=["ec=true"])
    aiko.compose_instance(ProbeActor, init_args)
    aiko.process.run()


def _devstack(*args):
    return subprocess.run(["bash", str(DEVSTACK), *args], cwd=str(REPO),
                          capture_output=True, text=True, timeout=120)


def _chatserver_pid() -> int | None:
    pidfile = WORK / "devstack.pids"
    if not pidfile.exists():
        return None
    pids = [int(x) for x in pidfile.read_text().split() if x.strip()]
    return pids[-1] if pids else None  # chatserver is launched last


def _wait_for(predicate, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    _log(f"TIMEOUT waiting for {what}")
    return False


def main() -> int:
    env = {**os.environ, "PATH": f"/opt/homebrew/sbin:{os.environ.get('PATH','')}"}
    _log("devstack down (idempotent)")
    subprocess.run(["bash", str(DEVSTACK), "down"], cwd=str(REPO), env=env,
                   capture_output=True, text=True, timeout=60)
    _log("devstack up")
    up = subprocess.run(["bash", str(DEVSTACK), "up"], cwd=str(REPO), env=env,
                        capture_output=True, text=True, timeout=120)
    if up.returncode != 0:
        _log(f"devstack up FAILED:\n{up.stdout}\n{up.stderr}")
        return 2
    time.sleep(3)  # let ChatServer register

    # Start the observer actor (stays alive across the whole test).
    threading.Thread(target=_run_aiko, name="aiko-probe", daemon=True).start()

    _phase["label"] = "initial"
    if not _wait_for(lambda: len([e for e in _events if e[2] == "add"]) >= 5, 25,
                     "initial 5 adds"):
        return 2
    time.sleep(2)
    initial_adds = len([e for e in _events if e[2] == "add"])
    _log(f"initial adds observed: {initial_adds}")

    # --- THE RESTART ---
    pid = _chatserver_pid()
    _phase["label"] = "restart"
    _log(f"killing ChatServer pid={pid}")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(4)  # let discovery_remove propagate
    _log("relaunching ChatServer (same workdir, same HyperSpace storage)")
    relaunch = subprocess.Popen(
        [str(AIKO_CHAT_BIN), "run"], cwd=str(CHAT_WORKDIR), env=env,
        stdout=open(LOGS / "chatserver_relaunch.log", "w"),
        stderr=subprocess.STDOUT)

    _phase["label"] = "recovery"
    # Watch for re-discovery + any removes for a generous window.
    _wait_for(lambda: any(d[2] == "add" and d[1] == "recovery" for d in _discovery),
              25, "re-discovery after restart")
    time.sleep(6)  # settle window to catch any late removes

    # --- REPORT ---
    _phase["label"] = "report"
    print("\n" + "=" * 60, flush=True)
    print("DECISION B VALIDATION — ChatServer restart", flush=True)
    print("=" * 60, flush=True)
    print(f"reconcile REMOVE events (the dangerous signal): {len(_removes)}", flush=True)
    for t, ph, name in _removes:
        print(f"   {t:6.2f}s [{ph}] remove {name}", flush=True)
    print(f"discovery transitions: "
          f"{[(round(t,1), ph, kind) for t, ph, kind, _ in _discovery]}", flush=True)
    adds_by_phase = {}
    for _, ph, cmd, _ in _events:
        if cmd == "add":
            adds_by_phase[ph] = adds_by_phase.get(ph, 0) + 1
    print(f"add events by phase: {adds_by_phase}", flush=True)

    try:
        relaunch.terminate()
    except Exception:
        pass
    try:
        aiko.process.terminate()
    except Exception:
        pass
    subprocess.run(["bash", str(DEVSTACK), "down"], cwd=str(REPO), env=env,
                   capture_output=True, text=True, timeout=60)

    if len(_removes) == 0:
        print("\nPASS: zero remove events across the restart. Decision B holds — "
              "a ChatServer restart cannot trigger a hard-delete.", flush=True)
        return 0
    print(f"\nFAIL: {len(_removes)} remove event(s) fired during a restart. "
          "Decision B must escalate to a debounce / N-stable-absence guard.",
          flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
