"""Bounded read-only HTTP perimeter; no forwarded client identities are trusted."""

import logging
import time
from collections import OrderedDict

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apps.api.config import Settings

logger = logging.getLogger("uvicorn.error")


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.clients: OrderedDict[str, tuple[float, int]] = OrderedDict()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500

        async def safe_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                    (b"x-bikeflow-release", self.settings.release.encode("ascii")),
                ]
            await send(message)

        async def reject(code: int) -> None:
            headers = {"Retry-After": "60"} if code == 429 else None
            await JSONResponse(
                {"error": {"code": "request_rejected", "message": "Request rejected"}},
                status_code=code,
                headers=headers,
            )(scope, receive, safe_send)

        try:
            if (
                len(scope.get("query_string", b"")) > 8192
                or sum(len(k) + len(v) for k, v in scope["headers"]) > 16384
            ):
                await reject(431)
                return
            # All application endpoints are read-only. Refuse request bodies,
            # including chunked JSON, before parsing/allocating a JSON document.
            for key, value in scope["headers"]:
                if key == b"transfer-encoding" or (
                    key == b"content-length" and value != b"0"
                ):
                    await reject(413)
                    return
            client = scope.get("client")
            address = str(client[0]) if client else "unknown"
            if scope["path"] not in {"/health", "/ready"}:
                since, count = self.clients.pop(address, (started, 0))
                if started - since >= 60:
                    since, count = started, 0
                self.clients[address] = (since, count + 1)
                if len(self.clients) > 1024:
                    self.clients.popitem(last=False)
                if count >= self.settings.rate_per_minute:
                    await reject(429)
                    return
            await self.app(scope, receive, safe_send)
        finally:
            # No path, query, headers, payload, DSN or exception messages in logs.
            logger.info(
                '{"event":"http","status":%d,"latency_ms":%.3f}',
                status,
                (time.monotonic() - started) * 1000,
            )
