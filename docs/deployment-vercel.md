# Deploying FreeFrame on Vercel

FreeFrame can run as a single Vercel project instead of the Docker stack. The
features are the same: uploads, transcoding, review, live updates, email and
the scheduled housekeeping. What changes is which service runs each part.

> **Status:** this deployment path is new. `vercel build` with the official
> CLI completes for both services (including the queue worker and crons), but
> it has not yet run against a live Vercel account.
> Try it on a staging project first. The Docker deployment
> ([deployment.md](deployment.md)) is unchanged and remains the reference.

---

## How the pieces map

| Docker service       | On Vercel                                                                 |
|----------------------|---------------------------------------------------------------------------|
| `web` (Next.js)      | `web` service (`apps/web`), served at `/`                                 |
| `api` (FastAPI)      | `api` service (repo root, `pyproject.toml`), served at `/api`             |
| Traefik              | Vercel routing (`vercel.json` rewrites; `/api` is stripped as before)     |
| `worker`, `email_worker`, `maintenance_worker` | One Celery queue subscriber on **Vercel Queues** (`apps/api/tasks/worker.py`) |
| FFmpeg in `worker`   | **Vercel Sandbox**, booted from the `apps/api/Dockerfile.prod` image       |
| `beat`               | **Vercel Cron** (`crons` in `vercel.json` → `/api/internal/cron/*`)        |
| `postgres`           | Any managed Postgres, e.g. **Neon** from the Vercel Marketplace            |
| `redis`              | Any Redis with a TCP URL, e.g. **Upstash** from the Vercel Marketplace     |
| `minio`              | External S3-compatible storage (AWS S3, Cloudflare R2, B2, …)              |
| `alembic upgrade head` | Runs during production builds (`apps/api/scripts/vercel_build.py`)       |

Redis is still needed for live updates (SSE pub/sub), magic codes and rate
limiting; it is no longer the Celery broker on Vercel.

---

## Before you start

- A **Vercel Pro** (or Enterprise) team is recommended. The hourly cron jobs
  need Pro (Hobby allows only daily jobs, see [Hobby plan](#hobby-plan)), and a
  sandbox can run for up to 5 hours on Pro versus 45 minutes on Hobby.
- **Vercel Queues** and **Vercel Sandbox** available on the team.
- An **S3-compatible bucket**. Vercel Blob is not S3-compatible, and uploads
  go straight from the browser to the bucket with presigned multipart URLs.
- **Email**: SES or any SMTP provider, as for Docker.
- **Docker** on your machine (or CI) to build the transcoding image.

---

## 1. Create the project

1. Import the repository in Vercel and, in **Build and Deployment**
   settings, set:
   - **Root Directory**: empty (the repository root). On import Vercel may
     suggest `apps/web` because it finds a Next.js app there; do not accept
     that, or `vercel.json` is ignored and only the web app is built (no API,
     no worker, no crons).
   - **Framework Preset**: **Services**. It is a project setting and cannot
     be set from `vercel.json`, which declares the `web` (Next.js) and `api`
     (FastAPI) services.

   Finish steps 2 and 3 before the first deploy: the API build checks for
   `DATABASE_URL`, `REDIS_URL` and `JWT_SECRET` (or their Neon/Upstash
   equivalents) and stops with a message naming any that are missing.

   A correct build log prefixes its lines with `[vc:service:web]` and
   `[vc:service:api]`. If you see `turbo run build` or
   `NEXT_OUTPUT_DIR_MISSING` instead, the Root Directory is still `apps/web`.
2. Set the **Production Branch**. For production use the `stable` branch once
   a release that includes this guide is on it (see
   [Release channels](../README.md#release-channels)).
3. From the Marketplace, add a **Postgres** database (Neon) and a **Redis**
   database (Upstash) to the project. They inject `POSTGRES_URL` / `DATABASE_URL`
   and `KV_URL` / `REDIS_URL`, which FreeFrame reads as is.

## 2. Build and push the transcoding image

Transcoding needs FFmpeg and ImageMagick and can run for hours, which a Vercel
Function cannot do. With `TRANSCODE_RUNNER=vercel_sandbox`, the queue worker
starts a Vercel Sandbox from this image and runs the same task inside it.

Vercel Sandbox boots images from the project's **Vercel Container Registry**
(VCR), and only `linux/amd64` ones. The `Worker image` workflow
(`.github/workflows/worker-image.yml`) builds `apps/api/Dockerfile.prod` and
pushes it there on every push to `main` that touches `apps/api/` or
`packages/transcoder/`, so the image keeps up with the API code. Run it by hand
from the Actions tab for the first image.

It signs in with GitHub OIDC, so no Vercel token is stored. Before the first
run:

1. In the workflow, set `VERCEL_TEAM_ID` (Team Settings → General) and the
   `IMAGE` path `vcr.vercel.com/<team-slug>/<project-slug>/freeframe-worker`.
2. In Vercel, open **Team Settings → Build and Deployment** and add an
   **OIDC policy** for VCR with read-write access, limited to this repository
   and `.github/workflows/worker-image.yml`.

When the repository shows **Ready** under **Project → Images**, set
`VERCEL_SANDBOX_IMAGE=freeframe-worker:latest`. A bare repository name resolves
in the deployment's own project.

To build by hand instead (Docker with Buildx and the Vercel CLI):

```bash
vercel link
vercel vcr login docker
vercel vcr build docker . freeframe-worker:latest --push -- -f apps/api/Dockerfile.prod
```

Then **rebuild whenever `apps/api/` or `packages/transcoder/` changes**, or the
sandbox runs older code than the deployment.

## 3. Environment variables

Set these in **Project → Settings → Environment Variables**. Nothing secret is
committed to the repository; `vercel env pull` writes a local `.env.local`,
which `.gitignore` already excludes.

| Variable | Required | Notes |
|----------|----------|-------|
| `DATABASE_URL` or `POSTGRES_URL` | yes | Set by the Neon integration. Use the pooled connection string. |
| `REDIS_URL` or `KV_URL` | yes | A `redis://` / `rediss://` TCP URL (not Upstash's REST URL). |
| `JWT_SECRET` | yes | `openssl rand -hex 64` |
| `S3_STORAGE`, `S3_BUCKET`, `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION` | yes | As in [External S3 Storage](deployment.md#external-s3-storage). For R2 and other non-AWS stores use `S3_STORAGE=minio`. |
| `MAIL_PROVIDER` and the `SMTP_*` / `AWS_MAIL_*` settings | yes | As in [External SMTP](deployment.md#external-smtp). |
| `CRON_SECRET` | yes | A long random string. Vercel Cron sends it; without it `/internal/cron/*` is off. |
| `TRANSCODE_RUNNER` | yes | `vercel_sandbox` |
| `VERCEL_SANDBOX_IMAGE` | yes | The image reference from step 2. |
| `DISABLE_DOCS` | recommended | `true` hides `/api/docs` in production. |
| `FRONTEND_URL` | optional | Defaults to the project's production domain. Set it when you use a custom domain. |
| `VERCEL_SANDBOX_VCPUS` | optional | vCPUs per sandbox (default 4). |
| `VERCEL_SANDBOX_TIMEOUT_MINUTES` | optional | Hard stop per sandbox (default 45; up to 300 on Pro). Raise it for long masters. |
| `SSE_MAX_STREAM_SECONDS` | optional | Length of one live-update connection (default 250 on Vercel). |
| `FREEFRAME_MIGRATE_ON_BUILD` | optional | `1` to migrate on preview builds too, `0` to never migrate on build. |
| `CELERY_BROKER_URL` | optional | Override the broker (default `vercel://` on Vercel). |

Every other setting in [`.env.example`](../.env.example) works the same as in
Docker.

## 4. Deploy

Deploy (push to the production branch, or `vercel --prod`). The production
build runs `alembic upgrade head` against the database; a failed migration
fails the build, so the deployment never goes live on the wrong schema.

Open the production URL. The first visit goes to `/setup` to create the
admin account, as on Docker.

The API sets the bucket's CORS rules from `FRONTEND_URL` when it starts. If
your storage provider ignores that API, allow your Vercel domain (and any
custom domain) as an origin on the bucket yourself.

---

## What is different on Vercel

- **Live updates reconnect every few minutes.** A function ends at its maximum
  duration, so each event stream closes cleanly after `SSE_MAX_STREAM_SECONDS`
  and the browser reconnects within about a second. An event published in that
  gap is not replayed; the page shows it on the next refresh, the same as when
  Redis drops a message.
- **Request bodies are limited to 4.5 MB** by Vercel. Media uploads are not
  affected (they go straight to the bucket), but a project poster image over
  4.5 MB is refused; the Docker limit is 10 MB.
- **Email and maintenance tasks share the function time limit** (300 s by
  default). An orphan sweep over a very large bucket may need several runs;
  Vercel Queues redelivers a task that did not finish.
- **A transcode that outlives the queue worker keeps going** in its sandbox
  and is stopped by `VERCEL_SANDBOX_TIMEOUT_MINUTES`. Its result reaches the
  app the usual way (database plus a live event). A sandbox that dies is
  picked up by the hourly `requeue-stuck-processing` job, as on Docker.
- **Queues deliver at least once.** A task can occasionally run twice. The
  housekeeping and transcode tasks are safe to repeat; a repeated email task
  sends a duplicate email.
- **Only `main` deploys from Git.** `vercel.json` turns off automatic
  deployments for every other branch (`git.deploymentEnabled`), because a
  preview would otherwise share production's database, or, with the
  variables set for Production only, fail its build. If production moves to
  another branch (such as `stable`), list that branch there instead.
- **To turn previews back on**, give them their own database (Neon branching
  per preview), Redis and `JWT_SECRET` for the Preview environment, set
  `FREEFRAME_MIGRATE_ON_BUILD=1` there so each branch database is migrated,
  and remove the `"*": false` rule. Preview builds do not migrate by default.
  Previews behind Vercel's deployment protection can't fetch branding on the
  server; it then loads in the browser, as before server rendering existed.
- **`NEXT_PUBLIC_BASE_PATH`** (sub-path deployments) is not supported on
  Vercel; use a domain or subdomain instead.

### Hobby plan

Vercel's Hobby plan only allows cron jobs that run once a day, and rejects a
deployment whose `vercel.json` has an hourly one. To deploy on Hobby, change
the three hourly schedules in `vercel.json` to daily ones (for example
`"0 2 * * *"`). Stale uploads and stuck transcodes are then reclaimed once a
day and due-date reminders arrive in a daily batch. Also keep
`VERCEL_SANDBOX_TIMEOUT_MINUTES` at 45 or less.

---

## Running the worker somewhere else

If you would rather not use Vercel Sandbox, run the Docker `worker` service
on any host instead and leave `TRANSCODE_RUNNER` at `inline` there. It must
reach the same broker as the Vercel functions: either point
`CELERY_BROKER_URL` at a Redis that both sides use (then the email and
maintenance queues also need a Docker worker, as in `docker-compose.prod.yml`),
or consume Vercel Queues from outside Vercel with the `vercel-poll://` broker
from the `vercel-celery` package and a Vercel token. With a Redis broker,
also remove the `[[tool.vercel.subscribers]]` block from `pyproject.toml`:
there is no Vercel queue for it to consume.

---

## Files involved

| File | Purpose |
|------|---------|
| `vercel.json` | Services, routing, cron schedules |
| `pyproject.toml` | API dependencies for Vercel (kept in step with `apps/api/requirements.txt`), the FastAPI entrypoint, the queue subscriber and the build hook |
| `apps/api/tasks/worker.py` | Queue subscriber entrypoint |
| `apps/api/routers/cron.py` | Cron endpoints that enqueue the beat tasks |
| `apps/api/services/vercel_sandbox.py`, `apps/api/tasks/sandbox_runner.py` | Transcoding in a sandbox |
| `apps/api/scripts/vercel_build.py` | Migrations at build time |
