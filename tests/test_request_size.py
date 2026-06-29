"""Request-body size cap (#28).

The middleware rejects oversized bodies with 413 via two paths: a declared
Content-Length fast-reject, and a bounded-buffer guard for chunked/lying requests.
Both must fire; legitimate small bodies and bodyless requests must pass untouched.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from aiko_gateway.middleware import ContentSizeLimitMiddleware

MAX = 100  # tiny cap for the test app


@pytest_asyncio.fixture
async def client():
    app = FastAPI()
    app.add_middleware(ContentSizeLimitMiddleware, max_bytes=MAX)

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {"len": len(body)}

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_body_under_cap_passes(client):
    r = await client.post("/echo", content=b"x" * 50)
    assert r.status_code == 200
    assert r.json() == {"len": 50}


@pytest.mark.asyncio
async def test_body_at_cap_passes(client):
    r = await client.post("/echo", content=b"x" * MAX)
    assert r.status_code == 200
    assert r.json() == {"len": MAX}


@pytest.mark.asyncio
async def test_oversized_content_length_rejected(client):
    # httpx sets Content-Length for a bytes body — exercises the fast-reject path.
    r = await client.post("/echo", content=b"x" * (MAX + 1))
    assert r.status_code == 413
    assert r.json() == {"detail": "request body too large"}


@pytest.mark.asyncio
async def test_oversized_chunked_rejected(client):
    # An async-iterator body makes httpx stream WITHOUT Content-Length (chunked),
    # exercising the bounded-buffer path rather than the header fast-reject.
    async def gen():
        for _ in range(20):
            yield b"x" * 10  # 200 bytes total, no Content-Length

    r = await client.post("/echo", content=gen())
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_no_body_request_passes(client):
    # Regression: the middleware must not break bodyless requests (GET/health/WS-ish).
    r = await client.get("/ping")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
