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


def main() -> int:
    if not should_migrate(dict(os.environ)):
        print(
            f"vercel-build: skipping migrations (VERCEL_ENV={os.environ.get('VERCEL_ENV')!r}; "
            "set FREEFRAME_MIGRATE_ON_BUILD=1 to run them on this build)"
        )
        return 0
    if not (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")):
        print("vercel-build: DATABASE_URL (or POSTGRES_URL) is not set; cannot migrate", file=sys.stderr)
        return 1
    print("vercel-build: alembic upgrade head")
    return subprocess.call([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_DIR)


if __name__ == "__main__":
    sys.exit(main())
