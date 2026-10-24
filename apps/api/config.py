import os
import re
from pathlib import Path
from urllib.parse import urlparse
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default S3 endpoint (local MinIO). Shared between the field default and the
# consistency validator so the two can't drift.
DEFAULT_S3_ENDPOINT = "http://minio:9000"

# Every token is signed and checked with JWT_SECRET as a shared HMAC key, so
# only the HMAC algorithms work. PyJWT fails an RS/ES algorithm on the first
# token rather than at startup.
JWT_ALGORITHMS = ("HS256", "HS384", "HS512")

TRANSCODE_RUNNERS = ("inline", "vercel_sandbox")


def _is_aws_endpoint(url: str) -> bool:
    """True if `url`'s host is an AWS S3 endpoint (an ``*.amazonaws.com`` host)."""
    host = (urlparse(url).hostname or "").lower()
    return host == "amazonaws.com" or host.endswith(".amazonaws.com")

# Find .env file - check current dir, then project root
# __file__ = apps/api/config.py, so parent.parent = project root
def _find_env_file() -> str:
    project_root = Path(__file__).parent.parent.parent  # freeframe/
    candidates = [
        Path(".env"),
        Path(".env.local"),
        project_root / ".env",
        project_root / ".env.local",
    ]
    for p in candidates:
        if p.exists():
            return str(p.resolve())
    return ".env"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_find_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore extra env vars not in model
        # Fields with an env alias (below) can still be passed by field name.
        populate_by_name=True,
    )

    # The second name in each alias pair is what Vercel Marketplace
    # integrations inject: Neon sets POSTGRES_URL, Upstash sets KV_URL. The
    # first (the name used everywhere else in this repo) wins when both exist.
    database_url: str = Field(validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL"))
    redis_url: str = Field(validation_alias=AliasChoices("REDIS_URL", "KV_URL"))
    # Celery broker. Unset means REDIS_URL, or Vercel Queues (`vercel://`)
    # when running on Vercel. See docs/deployment-vercel.md.
    celery_broker_url: str | None = None
    s3_storage: str = "minio"  # "s3" for AWS S3, "minio" for local MinIO
    s3_bucket: str = "freeframe"
    s3_endpoint: str = DEFAULT_S3_ENDPOINT
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"
    s3_public_endpoint: str | None = None  # External URL for presigned URLs (e.g. http://localhost:9000 when S3_ENDPOINT is http://minio:9000)
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    # Live event streams (GET /events/{project_id}). Authorization is checked
    # again for as long as a stream stays open, not only when it connects, so a
    # subscriber who loses access (removed from the project, deactivated, token
    # expired or revoked, project deleted or made private) stops receiving.
    # An event is never delivered on a check older than SSE_AUTH_MAX_AGE_SECONDS;
    # 0 checks before every single event. An idle stream is checked every
    # SSE_AUTH_RECHECK_SECONDS and sent a keepalive at that interval or every
    # 30 seconds, whichever is shorter. Each check is one indexed query. Token
    # expiry is exact either way: nothing is sent after the token's `exp`.
    sse_auth_max_age_seconds: float = Field(1.0, ge=0)
    sse_auth_recheck_seconds: float = Field(15.0, gt=0)
    # Longest a single event stream stays open, in seconds. The browser
    # reconnects on its own when a stream ends, so this only bounds one
    # connection, not the live view. 0 = no limit. Unset means no limit, or
    # 250 on Vercel, which ends a function at its maximum duration (300 s by
    # default) and would otherwise cut the stream mid-write.
    sse_max_stream_seconds: float | None = Field(None, ge=0)
    frontend_url: str = "http://localhost:3000"
    # Extra browser origins allowed by CORS, comma-separated (in addition to the
    # frontend + localhost defaults). Set to "*" to allow any origin — handy for
    # testing on a LAN via a machine's IP; do not use "*" in production.
    cors_allow_origins: str = ""
    transcoder_engine: str = "ffmpeg"
    # Which rungs of the quality ladder to build, comma-separated. Valid names
    # are the keys of packages.transcoder.ffmpeg_transcoder.QUALITY_MAP; the
    # default is the ladder that was hardcoded before this setting existed.
    # 2160p is opt-in and not part of it.
    #
    # An instance whose reviewers all watch at full size pays for the smaller
    # rungs in encode time and in storage without anyone playing them. Rungs
    # above the source resolution are still dropped, so this cannot be used to
    # ask for more than the source has: a ladder left empty that way builds its
    # smallest rung at the source's own size.
    transcoder_qualities: str = "1080p,720p,360p"

    # Maximum size (bytes) for a single uploaded file. 0 = unlimited (no per-file cap).
    # Note: S3 multipart still caps effective size at ~10,000 parts x chunk size.
    max_upload_bytes: int = 0

    # Reaper: uploads stuck in `uploading`/`failed` longer than this are reclaimed. Hours.
    # A version claimed as `processing` whose transcode never ran is re-dispatched
    # after this long. It must stay comfortably above the transcoder's own 4-hour
    # ceiling, or a legitimately slow encode gets a second worker on top of it.
    # 0 disables the sweep.
    stuck_processing_timeout_hours: int = 6
    stale_upload_timeout_hours: int = 24

    # Retention GC: rows soft-deleted (deleted_at) longer than this are hard-deleted and their
    # S3 objects reclaimed. Days. 0 (or negative) DISABLES the sweep (matches the reaper convention).
    soft_delete_retention_days: int = 30

    # Orphan S3 sweeper (issue #65 follow-up): reclaim bucket keys under raw/ + processed/ that no
    # MediaFile row owns. 0 = disabled. When > 0, only keys whose S3 LastModified is older than this
    # many hours are considered, so in-flight / just-committed uploads are never mistaken for orphans.
    orphan_sweep_grace_hours: int = 0
    # Report-only by default: when False the sweeper only LOGS what it would delete; set True to delete.
    orphan_sweep_delete: bool = False
    # Refuse to delete when more than this share of the keys scanned look orphaned. A database that
    # is empty, wrong or half-migrated makes the whole bucket look unowned, and that shape is a high
    # orphan ratio rather than a zero-length live-set, so the empty-set check alone does not catch it.
    # Raise it deliberately (1.0 disables the floor) for a first clean-up of a genuinely dirty bucket,
    # after reading the report-only output.
    orphan_sweep_max_orphan_ratio: float = 0.5

    # Where a transcode (process_asset, apply_watermark, the metadata
    # backfill) actually runs:
    #   inline          - in the Celery worker that picked it up (Docker; default)
    #   vercel_sandbox  - in a Vercel Sandbox started by that worker. For
    #                     Vercel, where a function has no FFmpeg and at most
    #                     a few minutes. See docs/deployment-vercel.md.
    transcode_runner: str = "inline"
    # Container image the sandbox boots: apps/api/Dockerfile.prod pushed to
    # the Vercel Container Registry. It carries FFmpeg, ImageMagick and this
    # code, so it must be rebuilt when the code changes.
    vercel_sandbox_image: str | None = None
    # Where the code lives inside that image (WORKDIR of Dockerfile.prod).
    vercel_sandbox_workdir: str = "/workspace"
    vercel_sandbox_vcpus: int = Field(4, ge=1)
    # Hard stop for one sandbox. Hobby allows 45 minutes, Pro up to 5 hours.
    vercel_sandbox_timeout_minutes: int = Field(45, ge=1)

    # Shared secret Vercel Cron sends as `Authorization: Bearer <CRON_SECRET>`
    # to /internal/cron/*. Unset disables those endpoints.
    cron_secret: str | None = None

    # Vercel BotID. The web app's /botid/verify route runs BotID when a sign-in
    # or share-password form loads and, for a human visitor, sets a short-lived
    # cookie signed with this key; the API then requires that cookie on the
    # endpoints in services/botid.py. 32 random bytes in hex
    # (`openssl rand -hex 32`), set for the web and API alike. Unset turns the
    # check off (Docker, local development).
    botid_cookie_secret: str | None = None

    # Worker concurrency settings
    transcoding_concurrency: int = 2  # Number of concurrent video transcoding jobs
    email_concurrency: int = 2  # Number of concurrent email sending jobs
    
    # Email settings - supports AWS SES or any SMTP server
    # If mail_provider is "ses", uses AWS SES with aws_mail_* credentials
    # If mail_provider is "smtp", uses standard SMTP with smtp_* settings
    mail_provider: str = "ses"  # "ses" or "smtp"
    mail_from_address: str = "noreply@example.com"
    # Blank means "follow the instance branding org name" (see
    # services/branding_service.resolve_org_name). Set it to pin a fixed
    # display name regardless of branding.
    mail_from_name: str = ""
    
    # AWS SES settings
    aws_mail_access_key_id: str | None = None
    aws_mail_secret_access_key: str | None = None
    aws_mail_region: str = "ap-south-1"
    
    # SMTP settings (for non-SES providers like SendGrid, Mailgun, self-hosted)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True

    @field_validator("jwt_secret")
    @classmethod
    def _check_jwt_secret(cls, value: str) -> str:
        """Refuse to start with an empty JWT_SECRET.

        PyJWT rejects an empty HMAC key on every encode and decode, which would
        show up as a 500 on sign-in and on every request carrying a bearer
        token. (python-jose accepted it, so anyone could sign a valid token.)
        """
        if not value.strip():
            raise ValueError(
                "JWT_SECRET is empty. Set it to a long random string, for "
                "example the output of `openssl rand -hex 64`."
            )
        return value

    @field_validator("botid_cookie_secret")
    @classmethod
    def _check_botid_cookie_secret(cls, value: str | None) -> str | None:
        """Blank means off; anything else must be 32 bytes of hex.

        The web app signs with the same rule, so a malformed key would make it
        refuse to issue cookies while the API demanded them, and every sign-in
        would fail. Failing at startup names the problem instead.
        """
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError(
                "BOTID_COOKIE_SECRET must be 32 random bytes in hex (64 characters), "
                "for example the output of `openssl rand -hex 32`."
            )
        return value

    @field_validator("jwt_algorithm")
    @classmethod
    def _check_jwt_algorithm(cls, value: str) -> str:
        if value not in JWT_ALGORITHMS:
            raise ValueError(
                f"JWT_ALGORITHM must be one of {', '.join(JWT_ALGORITHMS)}, not {value!r}. "
                f"Tokens are signed with JWT_SECRET as a shared key."
            )
        return value

    @model_validator(mode="after")
    def _default_frontend_url_on_vercel(self):
        """On Vercel, an unset FRONTEND_URL means the project's production domain.

        The web app and the API share one domain there, and Vercel exposes it
        as VERCEL_PROJECT_PRODUCTION_URL (a bare host). An explicit
        FRONTEND_URL, such as a custom domain, always wins.
        """
        if "frontend_url" not in self.model_fields_set and self.running_on_vercel:
            host = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
            if host:
                self.frontend_url = host if "://" in host else f"https://{host}"
        return self

    @model_validator(mode="after")
    def _check_s3_endpoint_consistency(self):
        """Fail loud on `S3_STORAGE=s3` + a real custom (non-AWS) `S3_ENDPOINT`.

        In `s3` mode the client talks to native AWS and S3_ENDPOINT is ignored,
        so pairing it with an R2/B2/MinIO URL would silently route to AWS. An
        untouched default and any `*.amazonaws.com` endpoint are harmless and
        allowed; a custom non-AWS endpoint is a misconfiguration.
        """
        if self.s3_storage.lower() == "s3":
            endpoint = (self.s3_endpoint or "").strip()
            if endpoint and endpoint != DEFAULT_S3_ENDPOINT and not _is_aws_endpoint(endpoint):
                raise ValueError(
                    f"S3_STORAGE=s3 selects native AWS S3 and ignores S3_ENDPOINT, but "
                    f"S3_ENDPOINT is set to a non-AWS URL ({endpoint!r}). To use a custom "
                    f"S3-compatible endpoint (MinIO, Cloudflare R2, Backblaze B2, "
                    f"DigitalOcean Spaces), set S3_STORAGE=minio. To use native AWS S3, "
                    f"leave S3_ENDPOINT unset."
                )
        return self

    @field_validator("transcode_runner")
    @classmethod
    def _check_transcode_runner(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in TRANSCODE_RUNNERS:
            raise ValueError(
                f"TRANSCODE_RUNNER must be one of {', '.join(TRANSCODE_RUNNERS)}, not {value!r}."
            )
        return value

    @property
    def running_on_vercel(self) -> bool:
        """True inside a Vercel build or function (Vercel sets VERCEL=1)."""
        return os.environ.get("VERCEL", "").strip() == "1"

    @property
    def celery_broker(self) -> str:
        """The broker Celery publishes to and consumes from."""
        if self.celery_broker_url:
            return self.celery_broker_url
        return "vercel://" if self.running_on_vercel else self.redis_url

    @property
    def uses_vercel_queues(self) -> bool:
        """Celery's broker is Vercel Queues (vercel://, vercel-push://, vercel-poll://)."""
        return self.celery_broker.startswith("vercel")

    @property
    def sse_stream_limit_seconds(self) -> float:
        """Longest one event stream may stay open; 0 = no limit."""
        if self.sse_max_stream_seconds is not None:
            return self.sse_max_stream_seconds
        return 250.0 if self.running_on_vercel else 0.0

    @property
    def frontend_origin(self) -> str:
        """`frontend_url` reduced to a bare `scheme://host[:port]` origin.

        A browser's Origin header never carries a path, so a sub-path
        deployment (FRONTEND_URL=https://host/freeframe) has to be matched
        against https://host — using the raw URL means no CORS check ever
        matches. Falls back to the configured value when it isn't parseable
        as an absolute URL.
        """
        from urllib.parse import urlparse
        raw = (self.frontend_url or "").strip()
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return raw
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def cors_allows_any_origin(self) -> bool:
        """CORS_ALLOW_ORIGINS contains "*"."""
        return "*" in [o.strip() for o in self.cors_allow_origins.split(",")]

    @property
    def cors_origins(self) -> list[str]:
        """Every browser origin allowed by CORS when `cors_allows_any_origin` is off.

        The single list behind both the API's CORS middleware (main.py) and the
        bucket CORS rules set at startup (s3_service.ensure_bucket_exists), so an
        origin allowed to call the API can also upload and play media. A store
        that enforces bucket CORS (Silo, Garage) refuses every other origin.
        """
        extra = [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]
        candidates = [
            # A sub-path deployment puts a path in FRONTEND_URL; an Origin
            # header never has one, so match against the bare origin.
            self.frontend_origin,
            "http://localhost:3000",
            "http://localhost:3001",
            *extra,
        ]
        return list(dict.fromkeys(o for o in candidates if o))

settings = Settings()
