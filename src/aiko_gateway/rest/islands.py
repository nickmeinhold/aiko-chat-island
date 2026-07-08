"""Public island directory endpoint (#1546; taxonomy #1760).

``GET /v1/islands`` returns THIS node's known island set (incl. self) as the
decentralized discovery layer — the app's server picker calls it to replace its
hardcoded preset list. Each entry is a peer ISLAND (the sovereign node: `id`,
`display_name`), addressed by its GATEWAY edge (`base_url`). No auth: the known
set is public discovery info (like /providers), and gossip reads the same endpoint.

``GET /v1/gateways`` is a DEPRECATED alias kept for the compat window: shipped app
builds and peers still on the pre-taxonomy build read it. Same data, old envelope
key (``gateways``). Remove once the app has adopted ``/v1/islands`` and old builds
have aged out (coordinate via #1760).

The trust model (peer poisoning is undefended; test-grade) lives in
domain/peers_service.py — read the banner there before relying on this.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..domain.peers_service import directory

router = APIRouter(prefix="/v1", tags=["islands"])


@router.get("/islands")
async def list_islands() -> dict:
    """The known island set: ``{"islands": [{"id", "display_name", "base_url"}, ...]}``
    — snake_case to match the app's reader. Each entry is a peer island; `base_url`
    is that island's gateway edge. Always includes this node's own entry (when a
    valid self identity is configured)."""
    return {"islands": [p.to_public() for p in directory.known()]}


@router.get("/gateways", deprecated=True)
async def list_gateways() -> dict:
    """DEPRECATED alias of ``GET /v1/islands`` (see module docstring). Same data,
    legacy envelope key ``gateways`` so pre-taxonomy app builds and gossip peers
    keep working through the compat window. Prefer ``/v1/islands``."""
    return {"gateways": [p.to_public() for p in directory.known()]}
