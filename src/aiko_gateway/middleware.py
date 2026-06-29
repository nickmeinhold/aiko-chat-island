"""App-level ASGI middleware (#28).

``ContentSizeLimitMiddleware`` rejects oversized request bodies before they reach
the route, with a deterministic 413. Strategy:

  1. **Fast reject on declared Content-Length** — if the header says the body
     exceeds the cap, 413 immediately without reading a byte. Every real client
     (httpx, the Dart ``http`` package, browsers) sends Content-Length, so this is
     the path that fires in practice.

  2. **Bounded buffer + replay** — for chunked requests (or a client lying about
     Content-Length), read the body in the middleware while counting bytes; abort
     with 413 the moment the running total exceeds the cap (so we never buffer more
     than the cap), otherwise replay the collected body to the app unchanged. This
     makes the limit deterministic regardless of how the body is framed and bounds
     the memory a malicious client can force us to hold.

The cap (``settings.max_request_bytes``) is generous for this gateway — auth
payloads (WebAuthn attestation/assertion, id_token JWTs) are a few KB and chat
messages are small text — so it never trips a legitimate request while capping the
abuse surface. There is no file-upload or streaming-body endpoint; WebSocket scopes
are passed through untouched.
"""
from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ContentSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Layer 1: declared Content-Length — reject before reading the body.
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await self._reject(send)
                        return
                except ValueError:
                    pass  # malformed header — fall through to the buffering guard
                break

        # Layer 2: buffer the body bounded by the cap, then replay it.
        chunks: list[bytes] = []
        total = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                # Client went away mid-body — hand the disconnect to the app and stop.
                async def _disconnected() -> Message:
                    return {"type": "http.disconnect"}
                await self.app(scope, _disconnected, send)
                return
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                await self._reject(send)
                return
            chunks.append(message.get("body", b""))
            more_body = message.get("more_body", False)

        body = b"".join(chunks)
        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    async def _reject(self, send: Send) -> None:
        body = b'{"detail":"request body too large"}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
