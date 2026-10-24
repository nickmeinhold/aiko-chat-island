"""Build the empty island the image ships: ``DB_URL=… python scripts/build_seed.py``.

The ONLY code that builds an island from nothing, and it is not part of the gateway:
it lives outside the package, ``docker build`` runs it once into an image layer
(``/app/seed/aiko.db``) and then deletes it, so nothing that starts, migrates or serves
an island can reach it. An island is born when standup copies that seed into a volume it
can prove it just created (Nick, 2026-10-25: "the standup executable does it"). Tests
import ``build`` to make the databases they start from.

The file is created with ``O_EXCL | O_NOFOLLOW`` on the path AS GIVEN: anything already
there (a database, a 0-byte file, a symlink, dangling or not) refuses, so even at build
time this can never be pointed at an island or redirected through a link (Noether,
Carnot, Tesla, PR#405 r1).
"""
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

from alembic import command

from aiko_gateway import migrate
from aiko_gateway.config import settings
from aiko_gateway.worker_guard import sqlite_file


def create_empty(db: Path) -> None:
    """An empty file is a valid, empty SQLite database, which the open-only connects
    may then build into."""
    try:
        fd = os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError as e:
        if not isinstance(e, FileExistsError) and e.errno != errno.ELOOP:
            raise
        raise RuntimeError(f"SEED_REFUSE_EXISTS: something is already at {db}.") from None
    os.close(fd)


def build(revision: str = "head") -> Path:
    """Create ``settings.db_url``'s file and build an empty island in it, to
    ``revision``. Reads the same setting as every other door, so the file it makes is
    the file they open. A URL with no file is refused: the gateway runs on SQLite in dev
    and prod (README), and an earlier revision that built into Postgres rested on a
    stale docstring (PR#405 r3)."""
    db = sqlite_file(settings.db_url, follow_symlinks=False)
    if db is None:
        raise RuntimeError(f"SEED_REFUSE_NO_FILE: {settings.db_url} names no SQLite file.")
    create_empty(db)
    command.upgrade(migrate._alembic_config(seed_build=True), revision)
    return db


if __name__ == "__main__":
    if sys.argv[1:]:
        raise SystemExit("usage: DB_URL=sqlite+aiosqlite:///<path> python scripts/build_seed.py")
    print(f"seed island built at {build()}")
