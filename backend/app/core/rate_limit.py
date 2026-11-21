"""Bounded, process-local rolling-window limits for development auth endpoints."""

from collections import deque
from collections.abc import Callable
from ipaddress import ip_address
from math import ceil
from threading import Lock
from time import monotonic

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

AUTH_LIMITS = {"/auth/login": 5, "/auth/register": 3}
AUTH_WINDOW_SECONDS = 60
AUTH_MAX_BUCKETS = 4096


class AuthRateLimiter:
    """Atomic admission with bounded storage; denied requests do not extend timeouts.

    Each admitted request counts regardless of validation or auth outcome. At
    capacity, expired buckets are reclaimed and new identities otherwise fail
    closed; active buckets are never evicted to make room for another client.
    """

    def __init__(
        self, *, clock: Callable[[], float] = monotonic, max_buckets: int = AUTH_MAX_BUCKETS,
    ) -> None:
        if max_buckets < 1:
            raise ValueError("max_buckets must be positive")
        self._clock = clock
        self._max_buckets = max_buckets
        self._buckets: dict[tuple[str, str], deque[float]] = {}
        self._lock = Lock()

    def retry_after(self, client: str, path: str) -> int:
        """Admit and return zero, or return the cooldown rounded up in seconds."""
        limit = AUTH_LIMITS[path]
        key = (client, path)
        with self._lock:
            now = self._clock()
            cutoff = now - AUTH_WINDOW_SECONDS
            bucket = self._buckets.get(key)
            if bucket is None:
                # Storage and cleanup work are both bounded by the bucket cap.
                for old_key, old_bucket in list(self._buckets.items()):
                    while old_bucket and old_bucket[0] <= cutoff:
                        old_bucket.popleft()
                    if not old_bucket:
                        del self._buckets[old_key]
                if len(self._buckets) >= self._max_buckets:
                    # A new identity needs an entire bucket to expire.
                    next_expiry = min(values[-1] for values in self._buckets.values())
                    return max(1, ceil(next_expiry + AUTH_WINDOW_SECONDS - now))
                bucket = self._buckets[key] = deque()
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, ceil(bucket[0] + AUTH_WINDOW_SECONDS - now))
            bucket.append(now)
            return 0


def client_identity(scope: Scope) -> str:
    """Use only the server-observed peer host; never parse forwarding headers.

    Run Uvicorn with --no-proxy-headers so scope['client'] is not rewritten.
    Missing peer information shares a conservative bucket instead of bypassing.
    """
    peer = scope.get("client")
    host = peer[0] if peer else "<unknown>"
    try:
        return str(ip_address(host))
    except ValueError:
        return host


class AuthRateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        # Starlette redirects trailing-slash variants before resolving routes.
        # Include those variants so redirects cannot create a parallel quota.
        if scope["type"] == "http" and scope["method"] == "POST" and path.rstrip("/") in AUTH_LIMITS:
            limiter = scope["app"].state.auth_rate_limiter
            retry = limiter.retry_after(client_identity(scope), path.rstrip("/"))
            if retry:
                response = JSONResponse(
                    {"detail": "Too many requests"}, status_code=429,
                    headers={"Retry-After": str(retry), "Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
