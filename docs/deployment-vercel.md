# Deploying FreeFrame on Vercel

FreeFrame can run as a single Vercel project instead of the Docker stack. The
features are the same: uploads, transcoding, review, live updates, email and
the scheduled housekeeping. What changes is which service runs each part.

> **Status:** this deployment path is new and has been checked against
> Vercel's builder source and SDKs, not yet against a live Vercel account.
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

1. Import the repository in Vercel. Leave **Root Directory** at the
   repository root; `vercel.json` sets the framework to **Services** and
   declares the `web` and `api` services.
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

```bash
docker build -f apps/api/Dockerfile.prod -t freeframe-worker:<git-sha> .
# Tag and push it to your project's Vercel Container Registry. See Vercel's
# Container Registry docs for `docker login`; the reference has the form
#   vcr.vercel.com/<team-slug>/<project-slug>/freeframe-worker:<git-sha>
docker tag freeframe-worker:<git-sha> vcr.vercel.com/<team>/<project>/freeframe-worker:<git-sha>
docker push vcr.vercel.com/<team>/<project>/freeframe-worker:<git-sha>
```

Set `VERCEL_SANDBOX_IMAGE` to that reference. The image contains the API code,
so **rebuild and re-point it whenever `apps/api/` or `packages/transcoder/`
changes**, or the sandbox runs older code than the deployment.

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
- **Preview deployments** do not migrate the database by default, because a
  preview usually shares production's database. With Neon branching per
  preview, set `FREEFRAME_MIGRATE_ON_BUILD=1` for the Preview environment.
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
