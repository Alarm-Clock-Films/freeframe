"""HLS proxy for secure video streaming.

Rewrites m3u8 manifests so that:
- Variant playlist URLs go through this proxy (with token auth)
- Segment (.ts) URLs become presigned S3 URLs (direct to S3)

This eliminates the need for a public bucket policy on processed/*.
"""

import base64
import hashlib
import logging
import posixpath
import zlib
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from ..config import settings
from ..services.s3_service import generate_presigned_get_url, get_s3_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["streaming"])


# Tokens end on the hour. Every token issued for a video within the same hour
# then ends at the same moment, so the playlists rewritten for one of them (the
# same segment links, signed until that moment) can be served to all of them.
HLS_TOKEN_EXPIRY_STEP_SECONDS = 3600


def create_hls_token(s3_prefix: str, expires_hours: int = 24) -> str:
    """Create a short-lived JWT for HLS proxy access.

    Good for `expires_hours`, rounded up to the next full hour: 24 to 25 hours
    by default.
    """
    step = HLS_TOKEN_EXPIRY_STEP_SECONDS
    earliest = datetime.now(timezone.utc).timestamp() + expires_hours * 3600
    expires_at = (int(earliest) // step + 1) * step
    payload = {
        "sub": "hls",
        "pfx": s3_prefix,
        "exp": expires_at,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


# The longest a segment URL is signed for: the longest create_hls_token gives a token.
SEGMENT_URL_MAX_SECONDS = 24 * 3600 + HLS_TOKEN_EXPIRY_STEP_SECONDS


def _decode_hls_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if payload.get("sub") != "hls":
        raise HTTPException(status_code=403, detail="Invalid token type")
    return payload


def _verify_hls_token(token: str) -> str:
    """Verify HLS token and return s3_prefix."""
    return _decode_hls_token(token)["pfx"]


def _segment_url_lifetime(payload: dict) -> int:
    """Seconds a segment URL signed now may live: what is left of the token, never
    more. The proxy checks only the token, so a segment URL that outlived it would
    keep video playing past the token's expiry -- after access to the asset ended."""
    exp = payload.get("exp")
    if exp is None:
        return SEGMENT_URL_MAX_SECONDS
    remaining = int(exp - datetime.now(timezone.utc).timestamp())
    return max(1, min(SEGMENT_URL_MAX_SECONDS, remaining))


def _rewrite_manifest(
    content: str,
    s3_prefix: str,
    manifest_path: str,
    token: str,
    segment_expires_in: int = SEGMENT_URL_MAX_SECONDS,
) -> str:
    """Rewrite URLs in an m3u8 manifest.

    - .m3u8 references -> proxy URLs with token (appended as query param)
    - .ts references -> presigned S3 URLs
    """
    manifest_dir = posixpath.dirname(manifest_path)
    lines = content.split("\n")
    result = []

    for line in lines:
        stripped = line.strip()

        # Pass through comments/tags and empty lines
        if not stripped or stripped.startswith("#"):
            result.append(line)
            continue

        # Resolve segment/playlist path relative to current manifest directory
        if manifest_dir:
            relative_key = f"{manifest_dir}/{stripped}"
        else:
            relative_key = stripped

        if stripped.endswith(".m3u8"):
            # Variant playlist -> proxy URL with token
            result.append(f"{relative_key}?token={token}")
        elif stripped.endswith(".ts"):
            # Segment -> presigned S3 URL (direct to S3), signed for what is
            # left of the token, so pause-and-resume works for as long as the
            # token does and no longer
            s3_key = f"{s3_prefix}/{relative_key}"
            result.append(generate_presigned_get_url(s3_key, expires_in=segment_expires_in))
        else:
            result.append(line)

    return "\n".join(result)


# ── Rewritten playlists, kept for an hour ────────────────────────────────────
# Stands where the token goes in a kept playlist; never valid in a JWT.
_TOKEN_PLACEHOLDER = "%%FREEFRAME-HLS-TOKEN%%"
_MANIFEST_CACHE_PREFIX = "hls_manifest:"
# No longer than the hour in which tokens share an expiry: after that no new
# token asks for this entry. If a version is transcoded again under the same
# prefix, viewers can be served the old playlist for up to this long.
HLS_MANIFEST_CACHE_SECONDS = HLS_TOKEN_EXPIRY_STEP_SECONDS


def _manifest_cache_key(s3_key: str, expires_at) -> str | None:
    """Where the playlist at `s3_key`, rewritten for tokens ending at
    `expires_at`, is kept. None when the token has no expiry to share."""
    if not isinstance(expires_at, (int, float)):
        return None
    from ..services.s3_service import _display_url_namespace
    digest = hashlib.sha256(f"{int(expires_at)}\n{s3_key}".encode()).hexdigest()
    return f"{_MANIFEST_CACHE_PREFIX}{_display_url_namespace()}:{digest}"


def _cached_manifest(cache_key: str | None) -> str | None:
    if cache_key is None:
        return None
    try:
        from ..services.redis_service import get_redis
        packed = get_redis().get(cache_key)
        if not packed:
            return None
        return zlib.decompress(base64.b64decode(packed)).decode("utf-8")
    except Exception:  # noqa: BLE001 - the cache is an optimisation; rewrite instead
        return None


def _store_manifest(cache_key: str | None, manifest: str, token_seconds_left: int) -> None:
    ttl = min(HLS_MANIFEST_CACHE_SECONDS, token_seconds_left)
    if cache_key is None or ttl <= 0:
        return
    try:
        from ..services.redis_service import get_redis
        # Signed links compress to a fraction of their size.
        packed = base64.b64encode(zlib.compress(manifest.encode("utf-8"))).decode("ascii")
        get_redis().set(cache_key, packed, ex=ttl)
    except Exception:  # noqa: BLE001 - see above
        pass


@router.get("/hls/{path:path}")
def hls_proxy(path: str, token: str = Query(...)):
    """Proxy HLS manifests with URL rewriting for secure streaming."""
    payload = _decode_hls_token(token)
    s3_prefix = payload["pfx"]

    # Only proxy m3u8 manifests
    if not path.endswith(".m3u8"):
        raise HTTPException(status_code=400, detail="Only .m3u8 files are proxied")

    # Prevent directory traversal
    normalised = posixpath.normpath(path)
    if normalised.startswith("..") or normalised.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")

    # Defense-in-depth: verify resolved key stays within the token's prefix
    s3_key = f"{s3_prefix}/{normalised}"
    if not s3_key.startswith(s3_prefix + "/"):
        raise HTTPException(status_code=400, detail="Invalid path")

    # A playlist already rewritten for a token that ends when this one does.
    # Rewriting signs a link for every segment (about a third of a second of
    # CPU for an hour of video, per quality level), and the links it holds are
    # signed until exactly that moment, so they are right for this token too.
    cache_key = _manifest_cache_key(s3_key, payload.get("exp"))
    cached = _cached_manifest(cache_key)
    if cached is None:
        # Fetch manifest from S3
        s3 = get_s3_client()
        try:
            obj = s3.get_object(Bucket=settings.s3_bucket, Key=s3_key)
            content = obj["Body"].read().decode("utf-8")
        except s3.exceptions.NoSuchKey:
            raise HTTPException(status_code=404, detail="Manifest not found")
        except Exception as e:
            logger.error("Failed to fetch HLS manifest %s: %s", s3_key, e)
            raise HTTPException(status_code=404, detail="Manifest not found")

        lifetime = _segment_url_lifetime(payload)
        cached = _rewrite_manifest(content, s3_prefix, normalised, _TOKEN_PLACEHOLDER, lifetime)
        _store_manifest(cache_key, cached, lifetime)

    # Links to other playlists carry the caller's own token.
    rewritten = cached.replace(_TOKEN_PLACEHOLDER, token)

    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )
