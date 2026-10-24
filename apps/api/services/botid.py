"""Vercel BotID for the endpoints bots go after: sign-in and share passwords.

BotID's server check, `checkBotId()`, exists only for JavaScript runtimes, and
on Vercel this API is a separate Python service. So the check runs in the web
app instead, following Vercel's "verify on page load" pattern
(https://vercel.com/docs/botid/verify-on-page-load):

1. Before a protected request, the page POSTs to the web app's /botid/verify
   route, which BotID protects on the client.
2. That route runs `checkBotId()` and, for a human visitor, sets
   `__Host-botid-verification`: an HS256 JWT signed with BOTID_COOKIE_SECRET
   that lives five minutes.
3. The browser sends the cookie with the request that follows, and
   `require_human` checks its signature, claims and age here.

The cookie only says that this browser passed BotID recently. Rate limits and
the endpoints' own checks stay in place. With BOTID_COOKIE_SECRET unset
(Docker, local development) the check is off.

The claims must match apps/web/lib/botid-cookie.ts.
"""
import time

import jwt
from fastapi import HTTPException, Request, status

from ..config import settings

COOKIE_NAME = "__Host-botid-verification"
ISSUER = "freeframe"
AUDIENCE = "botid-verification"
PURPOSE = "botid-verification"
MAX_AGE_SECONDS = 5 * 60
# Clock difference allowed between the web and API functions.
LEEWAY_SECONDS = 30

BOTID_REQUIRED = {
    "code": "botid_required",
    "message": "We couldn't confirm this request came from a person. Reload the page and try again.",
}


def botid_enabled() -> bool:
    return bool(settings.botid_cookie_secret)


def is_verified(token: str | None) -> bool:
    """Whether `token` is a verification cookie this deployment signed, still fresh."""
    if not token or not settings.botid_cookie_secret:
        return False
    try:
        claims = jwt.decode(
            token,
            bytes.fromhex(settings.botid_cookie_secret),
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            leeway=LEEWAY_SECONDS,
            options={"require": ["iat", "exp", "iss", "aud"]},
        )
    except jwt.PyJWTError:
        return False
    if claims.get("purpose") != PURPOSE:
        return False
    # `exp` is set by the issuer; cap the age here too, so a token minted with a
    # longer lifetime (a mistake on the web side) is still refused.
    issued = claims.get("iat")
    if not isinstance(issued, (int, float)):
        return False
    return time.time() - issued <= MAX_AGE_SECONDS + LEEWAY_SECONDS


def require_human(request: Request) -> None:
    """FastAPI dependency: refuse the request unless the browser passed BotID."""
    if not botid_enabled():
        return
    if not is_verified(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=BOTID_REQUIRED)
