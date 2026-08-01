"""Single-worker enforcement (until the #46 cross-worker sweep lands).

The realtime Hub keeps WS connections in per-PROCESS memory, so an
active-disconnect (ban / recovery re-key / account deletion) only reaches sockets
on the worker that handled it. Running more than one serving process (uvicorn
``--workers N``, ``WEB_CONCURRENCY``, or same-volume replicas) would let a banned
or deleted user's live socket on ANOTHER worker ride on until natural close.

We do NOT try to DETECT the worker count: uvicorn spawns its workers via
``multiprocessing`` (spawn), so a worker process's ``sys.argv`` is the spawn
bootstrap, not the ``uvicorn --workers 2`` command that created it — the worker
literally cannot see the flag. Instead we make concurrent serving structurally
impossible: every booting process contends for one exclusive advisory file lock
(``flock``). The first process acquires it and HOLDS it for its serving lifetime;
any second process fails to acquire and refuses to boot. This ENFORCES
single-worker serving (not merely alarms), and it catches the exact ``--workers``
case detection cannot — the flag's whole effect is more processes, and more
processes is precisely what contends on the lock.

SCOPE (honest): the lock lives on the SQLite data directory, so it is contended
across ``--workers`` processes AND replicas that share that volume ON THE SAME
HOST. It does NOT span hosts (a multi-host swarm with per-host volumes) — that
topology needs the #46 broker-level reconciliation, not a local lock. Lift the
guard with ``GATEWAY_ALLOW_MULTIWORKER=true`` once #46 is live
(docs/crucible/46-cross-worker-disconnect/CAST.md). ``fcntl`` is POSIX-only, which
matches the Linux container and macOS dev; there is no Windows serving target.
"""
from __future__ import annotations

import errno
import fcntl
import os
import tempfile
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on"}

# Keep the acquired lock's fd alive for the whole process: closing it (or letting
# it be GC'd) releases the flock. A module global is the process-lifetime anchor.
_lock_fd: int | None = None


def _multiworker_allowed() -> bool:
    return os.environ.get("GATEWAY_ALLOW_MULTIWORKER", "").strip().lower() in _TRUTHY


def _lock_path() -> Path:
    """Where the single-worker lock lives. GATEWAY_WORKER_LOCK overrides (tests)."""
    override = os.environ.get("GATEWAY_WORKER_LOCK")
    if override:
        return Path(override)
    from .config import settings

    url = settings.db_url
    # Absolute file-backed sqlite (prod): sqlite+aiosqlite:////data/aiko.db -> /data.
    # Co-locate the lock with the shared store so same-volume replicas contend too.
    if url.startswith("sqlite") and ":////" in url:
        db_path = "/" + url.split(":////", 1)[1]  # 4 slashes = absolute; restore one
        parent = Path(db_path).parent
        if parent.is_dir():
            return parent / ".aiko-worker.lock"
    # Fallback (relative/dev sqlite, or a future networked DB — neither runs
    # replicas): a stable temp path, still contended by --workers within a host.
    return Path(tempfile.gettempdir()) / "aiko-gateway.worker.lock"


def acquire_single_worker_lock() -> None:
    """Acquire the exclusive single-worker lock, or raise if another process holds it.

    No-op when GATEWAY_ALLOW_MULTIWORKER is set, or if already held by THIS process
    (idempotent — safe across a lifespan re-entry).
    """
    global _lock_fd
    if _multiworker_allowed() or _lock_fd is not None:
        return
    path = _lock_path()
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            raise RuntimeError(
                f"Refusing to boot: another gateway worker already holds the "
                f"single-worker lock ({path}). More than one serving process "
                "(uvicorn --workers N, WEB_CONCURRENCY, or same-volume replicas) is "
                "UNSAFE until the #46 cross-worker session-reconciliation sweep "
                "lands — the realtime Hub is per-process, so a ban/disconnect would "
                "not reach sockets on another worker. Run a single worker, or set "
                "GATEWAY_ALLOW_MULTIWORKER=true once #46 is live "
                "(docs/crucible/46-cross-worker-disconnect/CAST.md)."
            ) from e
        raise
    _lock_fd = fd


def release_single_worker_lock() -> None:
    """Release the lock (close the fd). Safe to call when never acquired."""
    global _lock_fd
    if _lock_fd is not None:
        os.close(_lock_fd)
        _lock_fd = None
