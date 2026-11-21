"""Centralized Origin and Fetch Metadata validation for unsafe HTTP requests."""

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import parse_origin, trusted_frontend_origins


class CSRFMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            headers = Headers(scope=scope)
            origins = headers.getlist("origin")
            sites = headers.getlist("sec-fetch-site")
            allowed = False
            try:
                allowed = (
                    len(origins) == 1
                    and parse_origin(origins[0]) in trusted_frontend_origins()
                    # This application supports only same-origin browser API calls.
                    and (not sites or sites == ["same-origin"])
                )
            except ValueError:
                # Bad headers or configuration fail closed without reflecting input.
                pass
            if not allowed:
                response = JSONResponse(
                    {"detail": "Forbidden"}, status_code=403,
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
