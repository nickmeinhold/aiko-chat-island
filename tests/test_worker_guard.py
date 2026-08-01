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
    # /tmp exists on the test host, so the data-dir branch is taken.
    assert worker_guard._lock_path() == __import__("pathlib").Path("/tmp/.aiko-worker.lock")
