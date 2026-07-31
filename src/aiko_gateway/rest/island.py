"""Signed island self-manifest endpoint (crucible-09 Phase A, A1).

``GET /v1/island`` returns THIS island's OWN signed identity + moderation posture —
distinct from ``GET /v1/islands`` (plural, ``rest/islands.py``), which is the
directory of PEERS this node has learned. The manifest is what a client fetches at
connect to learn, HONESTLY and before the user speaks, whether the operator can read
their messages (A3 renders it; A4 will carry the same signed fields into the peer
federation handshake).

No auth: an island's identity + mode are public (like ``/v1/islands`` and
``/providers``). The private key never leaves the process — only the public Multikey
and the signature are emitted, so a client (or later, a peer) can verify the manifest
was signed by the key the island claims.

The manifest is derived from the SAME canonical self identity the directory
advertises (``directory.self_peer`` — id/display_name/base_url, post-coercion) plus
the configured mode/key. Signing is per-request and cheap (Ed25519, ~tens of µs); the
self identity is immutable at runtime, so there is nothing to cache and no staleness
to manage.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from ..config import settings
from ..domain import island_identity
from ..domain.peers_service import directory

router = APIRouter(prefix="/v1", tags=["island"])


@router.get("/island")
async def get_island(response: Response) -> dict:
    """THIS island's signed self-manifest:
    ``{v, alg, id, display_name, base_url, mode, key_version, island_pubkey,
    signature}``. ``mode`` is the operator's elected moderation posture (Phase A:
    always ``moderator``); ``island_pubkey`` is a ``z…`` ed25519 Multikey; the
    signature covers the whole identity tuple (see domain/island_identity.py).

    503 if the island has no valid self identity configured (a broken
    gateway_base_url/id) — there is nothing authentic to sign, so fail closed rather
    than emit an unsigned or partial manifest."""
    self_peer = directory.self_peer
    if self_peer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="island self-identity is not configured "
                   "(no valid GATEWAY_BASE_URL / GATEWAY_ID)")
    # A signed trust document: never let a CDN/proxy freeze a stale mode/base_url
    # across a redeploy (mode is immutable per boot, so a restart is the only way it
    # changes — a cached copy could then advertise the OLD posture). no-store keeps
    # the posture a client sees current with the running process.
    response.headers["Cache-Control"] = "no-store"
    return island_identity.build_signed_manifest(
        id=self_peer.id,
        display_name=self_peer.display_name,
        base_url=self_peer.base_url,
        mode=settings.island_mode,
        key_version=settings.island_key_version,
        seed_b64url=settings.island_signing_seed,
    )
