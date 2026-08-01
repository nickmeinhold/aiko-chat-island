"""The island moderation-mode vocabulary — the SINGLE source of truth (crucible-09).

A leaf module (imports only stdlib `enum`) so BOTH `config.Settings.island_mode` and
`island_identity` (the signing/verify codec) name the same closed set. Two duplicated
string literals for one closed set was a real drift hazard — one silent edit and boot,
build, and verify would disagree about what a valid mode is. A `StrEnum` gives the
compiler/pydantic the closed set while still serializing to the plain wire string.

`e2ee` is schema-reserved for Phase B (MLS). It is a VALID vocabulary value a signed
manifest may name (so a future Phase B island / peer is legible), but config's
`_harden_for_production` HARD-REJECTS it at boot in Phase A — the boot guard, not the
vocabulary, owns the Phase-A-only policy.
"""
from __future__ import annotations

from enum import StrEnum


class IslandMode(StrEnum):
    MODERATOR = "moderator"
    E2EE = "e2ee"
