"""Single-worker enforcement (worker_guard) — the #46 interim guard.

The realtime Hub is per-process, so more than one serving worker is unsafe until
the #46 cross-worker sweep lands. worker_guard enforces this by contending on an
exclusive advisory file lock rather than trying to detect the worker count (which
uvicorn's spawn model hides). These tests exercise the lock contract directly:
a second acquirer of a held lock refuses to boot, the override lifts it, and the
happy path acquires/releases cleanly.

flock is associated with the OPEN FILE DESCRIPTION, so two separate `os.open` +
`flock(LOCK_EX)` on the SAME path contend even within one process — which is
exactly what a `--workers 2` launch does across two processes, so simulating it
in-process is faithful.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from aiko_gateway import worker_guard


@pytest.fixture(autouse=True)
def _clean_guard_state(monkeypatch, tmp_path):
    """Point the lock at a per-test temp file and reset module + env state."""
    lock = tmp_path / "worker.lock"
    monkeypatch.setenv("GATEWAY_WORKER_LOCK", str(lock))
    monkeypatch.delenv("GATEWAY_ALLOW_MULTIWORKER", raising=False)
    worker_guard.release_single_worker_lock()  # ensure no fd leaked from a prior test
    yield lock
    worker_guard.release_single_worker_lock()


def _hold(path) -> int:
    """Simulate a first worker holding the lock; returns the fd (caller closes)."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_second_worker_refuses_to_boot_when_lock_held(_clean_guard_state):
    # The acceptance gate: a second serving process (the `--workers 2` case) must
    # fail closed rather than start a second per-process Hub.
    held = _hold(_clean_guard_state)
    try:
        with pytest.raises(RuntimeError, match="single-worker lock"):
            worker_guard.acquire_single_worker_lock()
    finally:
        os.close(held)


def test_allow_multiworker_env_lifts_the_guard(monkeypatch, _clean_guard_state):
    # Explicit operator override (set once #46 is live) — the guard no-ops even
    # while another holder exists, so multi-worker can be enabled deliberately.
    monkeypatch.setenv("GATEWAY_ALLOW_MULTIWORKER", "true")
    held = _hold(_clean_guard_state)
    try:
        worker_guard.acquire_single_worker_lock()  # must NOT raise
    finally:
        os.close(held)


def test_acquire_then_release_happy_path(_clean_guard_state):
    # Sole worker: acquires the lock, holds it, then releases cleanly.
    worker_guard.acquire_single_worker_lock()
    assert worker_guard._lock_fd is not None
    worker_guard.release_single_worker_lock()
    assert worker_guard._lock_fd is None
    # After release, a fresh process (simulated by re-acquire) can take it.
    worker_guard.acquire_single_worker_lock()
    assert worker_guard._lock_fd is not None


def test_acquire_is_idempotent_within_process(_clean_guard_state):
    # A lifespan re-entry in the same process must not double-open or self-deadlock.
    worker_guard.acquire_single_worker_lock()
    fd = worker_guard._lock_fd
    worker_guard.acquire_single_worker_lock()  # no-op
    assert worker_guard._lock_fd == fd


def test_lock_path_prefers_the_sqlite_data_dir(monkeypatch):
    # Prod url sqlite+aiosqlite:////data/aiko.db → the lock lives beside the store
    # (so same-volume replicas contend), NOT in a private /tmp.
    monkeypatch.delenv("GATEWAY_WORKER_LOCK", raising=False)
    from aiko_gateway import config

    monkeypatch.setattr(config.settings, "db_url", "sqlite+aiosqlite:////tmp/aiko.db")
    # /tmp exists on the test host, so the data-dir branch is taken. resolve()
    # canonicalizes (/tmp -> /private/tmp on macOS), so compare against resolve() too.
    assert worker_guard._lock_path() == Path("/tmp/aiko.db").resolve().parent / ".aiko-worker.lock"


def test_lock_path_resolves_a_RELATIVE_sqlite_url(monkeypatch, tmp_path):
    # A relative dev url (3-slash) must resolve to a REAL directory, so two
    # independent gateways with different DBs never collide on one /tmp path
    # (cage-match PR#111, Kelvin). Run from a known cwd to make resolve() concrete.
    monkeypatch.delenv("GATEWAY_WORKER_LOCK", raising=False)
    monkeypatch.chdir(tmp_path)
    from aiko_gateway import config

    monkeypatch.setattr(config.settings, "db_url", "sqlite+aiosqlite:///./dev.db")
    assert worker_guard._lock_path() == tmp_path.resolve() / ".aiko-worker.lock"


def test_acquire_FAILS_CLOSED_when_no_shareable_path(monkeypatch):
    # In-memory sqlite (or a non-file backend) has no shareable data dir. The guard
    # must REFUSE to boot, not whisper into a private /tmp that wouldn't contend —
    # the fail-OPEN the guard exists to prevent (cage-match PR#111, Tesla).
    monkeypatch.delenv("GATEWAY_WORKER_LOCK", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_MULTIWORKER", raising=False)
    from aiko_gateway import config

    monkeypatch.setattr(config.settings, "db_url", "sqlite+aiosqlite:///:memory:")
    assert worker_guard._lock_path() is None
    with pytest.raises(RuntimeError, match="SHARED single-worker lock path"):
        worker_guard.acquire_single_worker_lock()


def test_allow_multiworker_bypasses_even_the_unshareable_path(monkeypatch):
    # The override must win before the fail-closed path check, so a Postgres/memory
    # deployment that has deliberately opted into multi-worker (post-#46) still boots.
    monkeypatch.setenv("GATEWAY_ALLOW_MULTIWORKER", "1")
    from aiko_gateway import config

    monkeypatch.setattr(config.settings, "db_url", "sqlite+aiosqlite:///:memory:")
    worker_guard.acquire_single_worker_lock()  # must NOT raise


def test_fork_child_is_not_fooled_by_inherited_globals(_clean_guard_state):
    # A fork child inherits _lock_fd/_lock_pid via the module global. The PID guard
    # must force it to re-acquire (not treat the parent's lock as its own), so it
    # then contends and refuses (cage-match PR#111, Tesla). Simulate by acquiring,
    # then spoofing a different owning PID and holding the lock from another fd.
    worker_guard.acquire_single_worker_lock()
    held_by_parent = worker_guard._lock_fd
    # Simulate being a fork child: same module globals, different os.getpid().
    worker_guard._lock_pid = worker_guard._lock_pid - 1  # pretend a different PID
    # A real second os.open+flock on the same path now contends with the still-open
    # parent fd, so the child's re-acquire refuses (the PID guard stopped it from
    # falsely short-circuiting on the inherited fd).
    with pytest.raises(RuntimeError, match="already holds"):
        worker_guard.acquire_single_worker_lock()
    # The refused re-acquire reset the globals to None; the parent's fd is still
    # open via our local ref. Close it so the fixture doesn't leak, then clear state.
    assert worker_guard._lock_fd is None
    os.close(held_by_parent)
    worker_guard._lock_pid = None
