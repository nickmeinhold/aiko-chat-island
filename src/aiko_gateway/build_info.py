"""What code is actually running in this container.

An island could not previously answer that about itself. `ISLAND_VERSION` exists
only as a compose-side variable on the box; nothing in the application ever read
it, so no endpoint named the running code. The question "which version is on that
box" was answerable only by SSH — and `deploy/update.sh` pulls the image without
syncing `docker-compose.yml` (#2301), so the file you'd read there is not
guaranteed to describe the container beside it.

A FILE, NOT AN ENVIRONMENT VARIABLE. This is the whole design. Provenance the host
can set is worthless: `environment:` in compose and `.env` on the box are the
OPERATOR's surface, and a value read from there reports the host's belief about the
artifact rather than the artifact. A JSON file written into an image layer at build
time is the BUILD's surface — changing it means rebuilding (or deliberately mounting
over it), not editing config. So `git_sha` here maps to a commit that really did
produce these bytes, which is the property `git archive`-staged deploys already give
us and which terminated, until now, before anything observable.

WHAT IT IS NOT: attested. This rides `/health` — public, unauthenticated, unsigned —
and a hostile operator can bake whatever sha they like. Attestable provenance means
the SIGNED manifest, and `island_identity` compares a manifest's key set to
`MANIFEST_KEYS` exactly, so an added key is a structural verification failure on
every peer: it is a `V` bump, deliberately deferred so one bump can carry both this
and #3731's media-posture split. `tests/test_build_info.py` holds the guard that
keeps the unsigned block out of the signed document until then.

The audience for the unsigned form is the operator auditing their own island, where
the adversary is a botched deploy rather than a lying host.

DISCLOSURE, NAMED RATHER THAN ASSUMED FREE: publishing an exact commit tells a
visitor precisely which code (and therefore which unpatched CVEs) an island runs.
Accepted deliberately — the repo and the image are already public, obscurity would
buy nothing real, and an island whose stated purpose is to be honest about what it
does to your messages cannot coherently hide what it is. The same reasoning the
`/v1/island` manifest already makes for moderation posture.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("aiko_gateway.build_info")

# Written by the Dockerfile from build ARGs. Absent in a source checkout, which is
# the common local case and is reported honestly as all-null rather than guessed at.
BUILD_INFO_PATH = Path("/app/build-info.json")

# The report's FIXED shape. Callers read these five keys whether or not the builder
# supplied them, so a consumer never has to test for an absent key — only a null one.
# Three codebases run inside one image (this repo plus the two pinned in the
# Dockerfile), and naming only ours would answer a third of the question: the
# aiko_chat ref in particular is a WIRE-CONTRACT lock that must match the bridge's.
FIELDS = ("git_sha", "ref", "built_at", "aiko_services_ref", "aiko_chat_ref")

# Bounded echo onto a public endpoint. A git sha is 40, a tag is short; 128 is slack
# for a long branch name. Over-length is REFUSED, never truncated — a truncated sha
# still looks like a sha and would be read as one.
MAX_VALUE_LEN = 128


def read_build_info(path: Path | None = None) -> dict[str, str | None]:
    """Read the baked provenance file into the fixed report shape.

    Total and non-raising by contract. `/health` is both the container liveness
    probe and `deploy/update.sh`'s post-deploy verification, so every failure here
    — absent file, unreadable file, corrupt JSON, wrong root type, junk values —
    degrades to `None` for that field. A corrupt provenance file must never be able
    to mark a working island unhealthy or fail a good deploy.

    Unknown keys are DROPPED rather than passed through: the file is a build input,
    and a build input must not be able to add arbitrary keys to a public response.
    """
    report: dict[str, str | None] = dict.fromkeys(FIELDS, None)
    try:
        raw = json.loads((path or BUILD_INFO_PATH).read_text())
    except FileNotFoundError:
        return report                      # source checkout — the honest common case
    except (OSError, ValueError):
        log.warning("build-info file present but unreadable; reporting null",
                    exc_info=True)
        return report
    if not isinstance(raw, dict):
        log.warning("build-info root is %s, not an object; reporting null",
                    type(raw).__name__)
        return report
    for field in FIELDS:
        value = raw.get(field)
        # `bool` is excluded explicitly: it is a subclass of int, not str, so it
        # would fail the isinstance check anyway — but stating it keeps this in the
        # same discipline as island_identity's bool-excluded numeric fields.
        if isinstance(value, str) and 0 < len(value) <= MAX_VALUE_LEN:
            report[field] = value
    return report


# Read ONCE at import. The file lives in an image layer and cannot change while the
# process runs, so re-reading per request would buy nothing and put a filesystem call
# on the liveness probe's path. Tests monkeypatch this attribute; `/health` must read
# it through the module (`build_info.BUILD_INFO`) rather than binding the name at
# import, or the patch would not be seen.
BUILD_INFO: dict[str, str | None] = read_build_info()
