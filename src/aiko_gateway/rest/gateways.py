"""Public island/gateway directory endpoint (#1546).

``GET /v1/gateways`` returns THIS gateway's known peer set (incl. self) as the
decentralized discovery layer — the app's server picker calls it to replace its
hardcoded preset list. No auth: the known-gateway set is public discovery info
(like /providers), and gossip between gateways reads the same endpoint.

The trust model (peer poisoning is undefended; test-grade) lives in
domain/peers_service.py — read the banner there before relying on this.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..domain.peers_service import directory

router = APIRouter(prefix="/v1", tags=["gateways"])


@router.get("/gateways")
async def list_gateways() -> dict:
    """The known peer set, camelCase per the #1546 contract:
    ``{"gateways": [{"id", "displayName", "baseURL"}, ...]}``. Always includes
    this gateway's own entry (when a valid self identity is configured)."""
    return {"gateways": [p.to_public() for p in directory.known()]}
