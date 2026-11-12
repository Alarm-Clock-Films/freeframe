"""
Global per-user / per-IP rate limiting middleware.

Applies to all API requests. Uses Redis sliding window counters.
- Authenticated users: keyed by user ID (from JWT)
- Unauthenticated requests: keyed by IP

Separate limits for read (GET/HEAD/OPTIONS) vs write (POST/PUT/PATCH/DELETE).

Plain ASGI with the asyncio Redis client. It used to be a BaseHTTPMiddleware
calling the blocking client from `async def dispatch`, so every request held
the event loop for its Redis round trips (three of them), and nothing else on
that worker ran meanwhile: not other requests' middleware, not the live event
streams. A slow Redis slowed everything; one that stopped answering stopped the
worker. The check is now one round trip that is awaited, with a short timeout.
"""

import asyncio
import weakref

import jwt
import redis.asyncio as aioredis
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..config import settings

# Limits per window — tuned for media-review workflows where a single folder
# page can trigger 10+ paginated asset fetches plus SWR calls for project,
# members, folders, etc., and bulk uploads generate many write requests.
READ_LIMIT = 600       # GET requests per window
WRITE_LIMIT = 300      # Mutating requests per window
WINDOW_SECONDS = 60    # 1-minute window

# Paths exempt from global rate limiting (they have their own)
EXEMPT_PATHS = {
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
}

# How long a request waits on Redis before it is let through uncounted. The
# limiter fails open, and a Redis that accepts connections but does not answer
# must not hold every request for the client's default (no timeout at all).
REDIS_TIMEOUT_SECONDS = 1.0

# One client per event loop: an asyncio connection belongs to the loop that
# opened it. A server process has one loop; tests start several.
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, aioredis.Redis]" = weakref.WeakKeyDictionary()


def get_redis() -> aioredis.Redis:
    """The asyncio Redis client for the running event loop."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
        )
        _clients[loop] = client
    return client


class GlobalRateLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        path = request.url.path

        # Skip exempt paths — uploads are auth-gated and have their own flow control
        if path in EXEMPT_PATHS or path.startswith("/stream/") or path.startswith("/upload/"):
            await self.app(scope, receive, send)
            return

        # Determine identity: user_id from JWT or IP
        identity = self._get_identity(request)

        # Determine limit based on method
        is_write = request.method in ("POST", "PUT", "PATCH", "DELETE")
        action = "global_w" if is_write else "global_r"
        limit = WRITE_LIMIT if is_write else READ_LIMIT

        # Check rate limit
        allowed, retry_after = await self._check(identity, action, limit)
        if not allowed:
            response = JSONResponse(
                status_code=429,
                content={"detail": f"Too many requests. Try again in {retry_after}s."},
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _get_identity(self, request: Request) -> str:
        """Extract user ID from JWT or fall back to IP."""
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                payload = jwt.decode(
                    token, settings.jwt_secret,
                    algorithms=[settings.jwt_algorithm],
                    options={"verify_exp": False},
                )
                user_id = payload.get("sub")
                if user_id:
                    return f"user:{user_id}"
            except jwt.InvalidTokenError:
                pass

        # Fall back to IP
        ip = request.headers.get("x-real-ip") or (
            request.client.host if request.client else "unknown"
        )
        return f"ip:{ip}"

    async def _check(self, identity: str, action: str, max_requests: int) -> tuple[bool, int]:
        try:
            key = f"grl:{action}:{identity}"
            # One round trip, one transaction: start the window if this is its
            # first request (SET NX EX), count this request, read what is left
            # of the window. Counted before it is compared, so requests sent
            # together cannot all pass on the same reading. Plain commands
            # only, so this works on any Redis, Upstash included.
            pipe = get_redis().pipeline(transaction=True)
            pipe.set(key, 0, nx=True, ex=WINDOW_SECONDS)
            pipe.incr(key)
            pipe.ttl(key)
            _, count, ttl = await pipe.execute()
            if int(count) > max_requests:
                return False, max(int(ttl), 1)
            return True, 0
        except Exception:
            # Fail open — allow the request if Redis is unavailable
            return True, 0
