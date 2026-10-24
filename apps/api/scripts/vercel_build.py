"""Vercel build step for the API service: apply database migrations.

Run by Vercel after the Python dependencies are installed (pyproject.toml,
`[tool.vercel.scripts] vercel-build`). It is what `alembic upgrade head` in the
Docker `api` command does, moved to deploy time because a function has no
startup step of its own.

Migrations run only where they are wanted:

- production builds (VERCEL_ENV=production) run them by default;
- preview and development builds skip them, since a preview usually shares
  the production database unless each branch gets its own (Neon branching);
- FREEFRAME_MIGRATE_ON_BUILD=1 forces them on, =0 forces them off.

A failed migration fails the build, so a deployment never goes live against a
schema it does not expect.

Before any of that, the build checks the variables the API cannot load
without. Vercel imports the app during the build (to find the Celery queues),
so a missing one would otherwise fail later with a long traceback.
"""
import os
import subprocess
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent  # apps/api, where alembic.ini lives


def should_migrate(environ: dict) -> bool:
    flag = environ.get("FREEFRAME_MIGRATE_ON_BUILD", "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no"):
        return False
    return environ.get("VERCEL_ENV") == "production"


# Each entry is satisfied by any one of its names.
REQUIRED_ENV = (
    ("DATABASE_URL", "POSTGRES_URL"),
    ("REDIS_URL", "KV_URL"),
    ("JWT_SECRET",),
)


def missing_env(environ: dict) -> list[str]:
    return [" or ".join(names) for names in REQUIRED_ENV
            if not any((environ.get(n) or "").strip() for n in names)]


def main() -> int:
    missing = missing_env(dict(os.environ))
    if missing:
        print(
            "vercel-build: ERROR: these environment variables are not set for this "
            f"environment ({os.environ.get('VERCEL_ENV') or 'unknown'}): {', '.join(missing)}.\n"
            "Add them in Project > Settings > Environment Variables (Postgres and Redis come "
            "from the Neon and Upstash integrations) and redeploy. "
            "See docs/deployment-vercel.md, step 3.",
            file=sys.stderr,
        )
        return 1
    if not should_migrate(dict(os.environ)):
        print(
            f"vercel-build: skipping migrations (VERCEL_ENV={os.environ.get('VERCEL_ENV')!r}; "
            "set FREEFRAME_MIGRATE_ON_BUILD=1 to run them on this build)"
        )
        return 0
    print("vercel-build: alembic upgrade head")
    return subprocess.call([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_DIR)


if __name__ == "__main__":
    sys.exit(main())
