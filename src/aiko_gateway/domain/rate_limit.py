"""Per-client rate limiting for the public auth endpoints (#28).

The gateway is a SINGLE uvicorn worker over file-backed SQLite, so an in-process
fixed-window counter is sufficient and needs no Redis. The asyncio event loop is
single-threaded and ``RateLimiter.hit`` performs NO ``await`` between reading and
writing its dict, so the read-modify-write is atomic with respect to other
requests — no lock is required. (If this service is ever scaled to multiple
workers the counter must move to shared storage; until then per-worker is the
whole population.)

Threat model: the public ceremonies (passkey/social/oauth/register/login) are
unauthenticated and some are crypto-expensive or account-creating. Without a limit
a single client can hammer them — credential-stuffing on /login, challenge-table
growth on the passkey ceremonies, broker-quota burn on /oauth. The limit is a
blast-radius cap, NOT an authn control.
"""
from __future__ import annotations

import time

from fastapi import Depends, HTTPException, Request, status

from ..config import settings

# Bound the key dict so a spray of distinct client IPs can't grow it without limit.
# Far above any real concurrent-client count for this gateway; eviction of expired
# windows (below) keeps the live set near the active-client count in practice.
_MAX_KEYS = 100_000


def client_ip(request: Request) -> str:
    """The untrusted client's IP, used as the rate-limit key.

    We sit behind EXACTLY ONE trusted reverse proxy — Caddy on the host loopback
    (``chat.imagineering.cc { reverse_proxy localhost:8095 }``, no ``trusted_proxies``
    directive). In that default mode Caddy APPENDS the immediate peer's IP to
    ``X-Forwarded-For``. So the trustworthy value is the RIGHTMOST entry (the one
    Caddy itself added); everything to its left is attacker-suppliable and MUST NOT
    key the limiter — otherwise a client sends ``X-Forwarded-For: <random>`` and
    mints a fresh budget per request, evading the limit entirely.

    Fall back to ``request.client.host`` only when XFF is absent (direct-to-app:
    local dev, tests, or a future non-Caddy ingress). If the proxy topology ever
    changes (more than one hop, or Caddy gains ``trusted_proxies``), revisit which
    index is trustworthy — this is the security hinge of the whole module.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Fixed-window request counter keyed by (bucket, client-ip)."""

    def __init__(self) -> None:
        # (bucket, ip) -> (window_start_monotonic, count)
        self._windows: dict[tuple[str, str], tuple[float, int]] = {}

    def reset(self) -> None:
        """Drop all state. Used by tests for per-test isolation."""
        self._windows.clear()

    def hit(self, bucket: str, ip: str, limit: int, window: float) -> tuple[bool, int]:
        """Record one request. Returns ``(allowed, retry_after_seconds)``.

        A request is allowed while the count within the current ``window`` seconds
        is ``<= limit``; the (limit+1)-th is rejected with a Retry-After equal to
        the seconds left in the window.
        """
        now = time.monotonic()
        key = (bucket, ip)
        start, count = self._windows.get(key, (now, 0))
        if now - start >= window:
            start, count = now, 0  # window elapsed — reset
        count += 1
        self._windows[key] = (start, count)
        if count > limit:
            retry = int(window - (now - start)) + 1
            return False, max(retry, 1)
        if len(self._windows) > _MAX_KEYS:
            self._evict(now, window)
        return True, 0

    def _evict(self, now: float, window: float) -> None:
        """Drop windows that have fully elapsed (their next hit would reset anyway).
        Opportunistic — only runs when the dict grows past the cap."""
        stale = [k for k, (start, _) in self._windows.items() if now - start >= window]
        for k in stale:
            del self._windows[k]


# Module-global limiter — one population per worker process.
limiter = RateLimiter()


def rate_limit(bucket: str):
    """Build a FastAPI dependency that rate-limits the route by client IP.

    Usage: ``@router.post(..., dependencies=[Depends(rate_limit("passkey"))])``.
    Routes sharing a ``bucket`` share one per-IP budget (e.g. all four passkey
    ceremony endpoints share "passkey", so an attacker can't get 4x the budget by
    rotating endpoints). Disabled wholesale by ``settings.rate_limit_enabled``.
    """
    async def _dependency(request: Request) -> None:
        if not settings.rate_limit_enabled:
            return
        allowed, retry_after = limiter.hit(
            bucket,
            client_ip(request),
            settings.auth_rate_limit,
            settings.auth_rate_limit_window_seconds,
        )
        if not allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded; slow down",
                headers={"Retry-After": str(retry_after)},
            )

    return Depends(_dependency)
