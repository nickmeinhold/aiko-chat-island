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

SCOPE / failure model (cage-match PR#111, Kelvin+Carnot+Tesla):
- The lock lives beside the SQLite data file (via SQLAlchemy URL parsing, so BOTH
  relative dev paths and absolute prod paths resolve to their real directory).
  Two independent gateways with different DBs get different lock dirs (no false
  collision); same-volume replicas on ONE host share the dir and contend.
- FAIL CLOSED, never fail open: if a shareable path cannot be proven (in-memory
  sqlite, or a non-file backend like Postgres), acquire RAISES rather than
  whispering into a private ``/tmp`` path that wouldn't contend. Set
  ``GATEWAY_WORKER_LOCK`` (an explicit shared path) or ``GATEWAY_ALLOW_MULTIWORKER``
  to proceed.
- Does NOT span hosts. ``flock`` on a network filesystem (NFS/EFS) is
  environment-dependent — with ``local_lock`` emulation it locks only per client
  host, so multiple hosts could each boot a worker. Multi-host needs the #46
  broker-level reconciliation, not a local lock. These islands use local Docker
  volumes, where ``flock`` is authoritative.
- Assumes uvicorn's spawn model (each worker acquires its OWN fd post-spawn). A
  fork-without-exec child would inherit the fd; the PID guard below forces such a
  child to re-acquire (and thus refuse). ``fcntl`` is POSIX-only, matching the
  Linux container and macOS dev; there is no Windows serving target.

Lift the guard with ``GATEWAY_ALLOW_MULTIWORKER=true`` once #46 is live
(docs/crucible/46-cross-worker-disconnect/CAST.md).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import os
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on"}

# Keep the acquired lock's fd alive for the whole process: closing it (or letting
# it be GC'd) releases the flock. The PID pins the fd to the process that opened
# it, so a fork child (which inherits the module global) is not fooled into
# thinking it already holds the lock.
_lock_fd: int | None = None
_lock_pid: int | None = None


def _multiworker_allowed() -> bool:
    return os.environ.get("GATEWAY_ALLOW_MULTIWORKER", "").strip().lower() in _TRUTHY


def _lock_path() -> Path | None:
    """The single-worker lock path, or None when no shareable path can be proven.

    None → acquire fails CLOSED (see module docstring). GATEWAY_WORKER_LOCK forces
    an explicit path (used by tests and by non-file DB backends).
    """
    override = os.environ.get("GATEWAY_WORKER_LOCK")
    if override:
        return Path(override)
    from sqlalchemy.engine import make_url

    from .config import settings

    url = make_url(settings.db_url)
    # A file-backed sqlite DB (relative OR absolute) has a real directory we can
    # co-locate the lock with. resolve() normalizes `./aiko_dev.db` and
    # `////data/aiko.db` alike, so different DBs never share a lock and same-volume
    # replicas do. In-memory sqlite and non-file backends have no such directory.
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        parent = Path(url.database).resolve().parent
        if parent.is_dir():
            return parent / ".aiko-worker.lock"
    return None


def acquire_single_worker_lock() -> None:
    """Acquire the exclusive single-worker lock, or raise if it cannot be held alone.

    Raises RuntimeError when another process holds the lock, when no shareable lock
    path can be proven, or on an unexpected filesystem lock error (fail closed).
    No-op when GATEWAY_ALLOW_MULTIWORKER is set, or if already held by THIS process
    (idempotent across a lifespan re-entry; a fork child has a different PID and so
    is forced to re-acquire).
    """
    global _lock_fd, _lock_pid
    if _multiworker_allowed():
        return
    if _lock_fd is not None and _lock_pid == os.getpid():
        return
    # A fork child inherited the module globals but is a NEW process. Forget the
    # inherited reference so it must acquire on its own — its fresh flock attempt
    # then contends with the parent's still-held lock and correctly refuses boot.
    # (Don't close the inherited fd: it shares the parent's open file description.)
    _lock_fd = None
    _lock_pid = None

    path = _lock_path()
    if path is None:
        raise RuntimeError(
            "Refusing to boot: cannot determine a SHARED single-worker lock path "
            "(settings.db_url is in-memory sqlite or a non-file backend). The "
            "realtime Hub is per-process, so >1 worker is unsafe until the #46 "
            "cross-worker sweep lands. Set GATEWAY_WORKER_LOCK to a path on the "
            "shared data volume, or GATEWAY_ALLOW_MULTIWORKER=true once #46 is live "
            "(docs/crucible/46-cross-worker-disconnect/CAST.md)."
        )
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        # e.g. EACCES: the lock file exists owned by another OS user. Fail closed
        # with context rather than a raw traceback.
        raise RuntimeError(
            f"Refusing to boot: cannot open the single-worker lock file {path} "
            f"({e.strerror}). Check ownership/permissions on the data volume, or "
            "set GATEWAY_WORKER_LOCK to a writable shared path."
        ) from e
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
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
        # ENOLCK / EOPNOTSUPP (e.g. a network filesystem that can't lock): fail
        # closed with context rather than silently proceeding unlocked.
        raise RuntimeError(
            f"Refusing to boot: the single-worker lock ({path}) could not be taken "
            f"({e.strerror}). If the data dir is a network filesystem, flock may be "
            "unsupported — use a local volume, or set GATEWAY_WORKER_LOCK to a "
            "lockable path, or GATEWAY_ALLOW_MULTIWORKER=true if #46 is live."
        ) from e
    _lock_fd = fd
    _lock_pid = os.getpid()


def release_single_worker_lock() -> None:
    """Release the lock (close the fd). Safe to call when never acquired.

    Only ever called after all serving state (bus, hub tasks) is torn down, so the
    brief window before process exit serves nothing. The authoritative backstop is
    still process death: the kernel closes the fd (and releases the flock) even if
    this is never reached — e.g. a hard crash.
    """
    global _lock_fd, _lock_pid
    if _lock_fd is not None and _lock_pid == os.getpid():
        with contextlib.suppress(OSError):
            os.close(_lock_fd)
    _lock_fd = None
    _lock_pid = None
