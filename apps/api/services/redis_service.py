import redis
import secrets
from typing import Optional
from ..config import settings

# Redis client
_redis_client: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    """Get Redis client singleton."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


# Magic code keys
MAGIC_CODE_PREFIX = "magic_code:"
MAGIC_CODE_ATTEMPTS_PREFIX = "magic_code_attempts:"
MAGIC_CODE_EXPIRY_SECONDS = 600  # 10 minutes
MAX_MAGIC_CODE_ATTEMPTS = 5


def generate_magic_code() -> str:
    """Generate a 6-digit magic code."""
    return str(secrets.randbelow(900000) + 100000)


def store_magic_code(email: str, code: str) -> None:
    """Store magic code in Redis with expiry."""
    r = get_redis()
    key = f"{MAGIC_CODE_PREFIX}{email.lower()}"
    r.setex(key, MAGIC_CODE_EXPIRY_SECONDS, code)
    # Reset attempts counter
    attempts_key = f"{MAGIC_CODE_ATTEMPTS_PREFIX}{email.lower()}"
    r.delete(attempts_key)


def verify_magic_code(email: str, code: str) -> tuple[bool, str]:
    """
    Verify magic code from Redis.
    Returns (success, error_message).
    """
    r = get_redis()
    key = f"{MAGIC_CODE_PREFIX}{email.lower()}"
    attempts_key = f"{MAGIC_CODE_ATTEMPTS_PREFIX}{email.lower()}"
    
    # Check attempts
    attempts = r.get(attempts_key)
    if attempts and int(attempts) >= MAX_MAGIC_CODE_ATTEMPTS:
        return False, "Too many attempts. Request a new code."
    
    # Get stored code
    stored_code = r.get(key)
    if not stored_code:
        return False, "Code expired or not found"
    
    if stored_code != code:
        # Increment attempts
        r.incr(attempts_key)
        r.expire(attempts_key, MAGIC_CODE_EXPIRY_SECONDS)
        return False, "Invalid code"
    
    # Success - delete the code
    r.delete(key)
    r.delete(attempts_key)
    return True, ""


def delete_magic_code(email: str) -> None:
    """Delete magic code from Redis."""
    r = get_redis()
    key = f"{MAGIC_CODE_PREFIX}{email.lower()}"
    attempts_key = f"{MAGIC_CODE_ATTEMPTS_PREFIX}{email.lower()}"
    r.delete(key)
    r.delete(attempts_key)


# Invite token keys (also in Redis for faster lookup)
INVITE_TOKEN_PREFIX = "invite_token:"
INVITE_TOKEN_EXPIRY_SECONDS = 7 * 24 * 60 * 60  # 7 days


def store_invite_token(token: str, user_id: str) -> None:
    """Store invite token -> user_id mapping in Redis."""
    r = get_redis()
    key = f"{INVITE_TOKEN_PREFIX}{token}"
    r.setex(key, INVITE_TOKEN_EXPIRY_SECONDS, user_id)


def get_user_id_from_invite_token(token: str) -> Optional[str]:
    """Get user_id from invite token."""
    r = get_redis()
    key = f"{INVITE_TOKEN_PREFIX}{token}"
    return r.get(key)


def delete_invite_token(token: str) -> None:
    """Delete invite token from Redis."""
    r = get_redis()
    key = f"{INVITE_TOKEN_PREFIX}{token}"
    r.delete(key)


# ── IP-based rate limiting ────────────────────────────────────────────────────

RATE_LIMIT_PREFIX = "rl:"


def check_rate_limit(
    ip: str,
    action: str,
    max_requests: int,
    window_seconds: int,
) -> tuple[bool, int]:
    """
    Check if an IP has exceeded the rate limit for a given action.
    Returns (allowed, remaining_seconds_until_reset).
    Uses a simple counter with TTL in Redis. Fails open if Redis is unavailable.
    """
    try:
        r = get_redis()
        key = f"{RATE_LIMIT_PREFIX}{action}:{ip}"
        current = r.get(key)

        if current is not None and int(current) >= max_requests:
            ttl = r.ttl(key)
            return False, max(ttl, 1)

        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        pipe.execute()
        return True, 0
    except Exception:
        # Fail open — allow the request if Redis is unavailable
        return True, 0


# ── Per-address limit on sign-in code emails ──────────────────────────────────

MAGIC_CODE_SEND_PREFIX = "magic_code_sends:"


def reserve_magic_code_send(email: str) -> bool:
    """Count one sign-in code email to `email` against that address's limits.

    Returns False when the address is over a limit, and then no email may be
    sent. Unlike the per-IP limit this holds however many networks the
    requests come from, so a known address cannot be flooded, and the mail
    provider's daily quota cannot be spent on one address.

    The limits (config.magic_code_*) are a minimum gap between emails and caps
    per hour and per day; 0 turns one off. Each window starts with the first
    email in it. Every request that clears the gap counts, including one then
    refused by a cap, so retrying in a loop does not help.

    Fails open, as check_rate_limit does: without Redis no code can be stored
    either, and the request fails there with an error rather than silently
    sending nothing.
    """
    address = email.strip().lower()
    try:
        r = get_redis()
        gap = settings.magic_code_min_interval_seconds
        if gap > 0 and not r.set(f"{MAGIC_CODE_SEND_PREFIX}gap:{address}", "1", nx=True, ex=gap):
            return False
        for window, limit in (
            (3600, settings.magic_code_max_per_hour),
            (86400, settings.magic_code_max_per_day),
        ):
            if limit <= 0:
                continue
            key = f"{MAGIC_CODE_SEND_PREFIX}{window}:{address}"
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, window, nx=True)
            count, _ = pipe.execute()
            if count > limit:
                return False
        return True
    except Exception:  # noqa: BLE001 - fail open; see the docstring
        return True


# ── Share link password sessions ──────────────────────────────────────────────

SHARE_SESSION_PREFIX = "share_session:"
SHARE_SESSION_EXPIRY_SECONDS = 3600  # 1 hour


def create_share_session(token: str, session_id: str) -> None:
    """Store a session after successful password verification."""
    r = get_redis()
    key = f"{SHARE_SESSION_PREFIX}{token}:{session_id}"
    r.setex(key, SHARE_SESSION_EXPIRY_SECONDS, "1")


def verify_share_session(token: str, session_id: str) -> bool:
    """Check if a valid password session exists for this share link."""
    r = get_redis()
    key = f"{SHARE_SESSION_PREFIX}{token}:{session_id}"
    return r.exists(key) > 0
