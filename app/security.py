"""Bound request sizes and reject excess concurrent work before database/AI access."""

import asyncio
from starlette.responses import JSONResponse


class RequestGuard:
    def __init__(self, app, max_body=65536, max_active=40):
        self.app = app
        self.max_body = max_body
        self.max_active = max_active
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.active >= self.max_active:
            return await JSONResponse(
                {"detail": "Server busy. Please retry shortly."},
                503,
                headers={"Retry-After": "5"},
            )(scope, receive, send)
        max_body = (
            2097152
            if scope.get("path", "").endswith("/photo")
            and scope.get("method") == "POST"
            else self.max_body
        )
        self.active += 1
        try:
            headers = dict(scope.get("headers", []))
            length = headers.get(b"content-length")
            if length:
                try:
                    size = int(length)
                except ValueError:
                    size = -1
                if size < 0:
                    return await JSONResponse(
                        {"detail": "Invalid Content-Length."}, 400
                    )(scope, receive, send)
                if size > max_body:
                    return await JSONResponse(
                        {"detail": "Request body too large."}, 413
                    )(scope, receive, send)
            chunks = []
            total = 0
            while True:
                try:
                    event = await asyncio.wait_for(receive(), timeout=10)
                except TimeoutError:
                    return await JSONResponse(
                        {"detail": "Request body timed out."}, 408
                    )(scope, receive, send)
                if event["type"] == "http.disconnect":
                    return
                if event["type"] != "http.request":
                    continue
                total += len(event.get("body", b""))
                if total > max_body:
                    return await JSONResponse(
                        {"detail": "Request body too large."}, 413
                    )(scope, receive, send)
                chunks.append(event.get("body", b""))
                if not event.get("more_body", False):
                    break
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": b"".join(chunks),
                        "more_body": False,
                    }
                return await receive()

            await self.app(scope, replay, send)
        finally:
            self.active -= 1
