# Syncing the fork with upstream

Use this when GitHub says the fork is "N commits behind Techiebutler/freeframe:main",
or when asked to sync, update or merge from upstream or to pick up a new upstream release.

The fork carries its own changes (Vercel deployment, BotID, security headers,
sign-in code limits, branding first-paint, accent text colours, Delete forever
in the trash, and more). Upstream keeps shipping releases. The goal is always:
**keep all fork changes, add all upstream changes, land it through a PR.**

Last done: 2026-10-25, bringing in v1.21.0 (PR Alarm-Clock-Films/freeframe#17).

## 1. Measure the gap

```bash
git remote add upstream https://github.com/Techiebutler/freeframe 2>/dev/null
git fetch origin main && git fetch upstream main --tags
git rev-list --left-right --count origin/main...upstream/main   # ahead / behind
git log --oneline --no-merges upstream/main..origin/main          # fork only
git log --oneline --no-merges origin/main..upstream/main          # upstream only
git tag --contains upstream/main                                  # which release
```

## 2. Merge, never rebase

Work on the session's designated branch, starting from `origin/main`:

```bash
git checkout -B <branch> origin/main
git merge --no-ff --no-commit upstream/main
git diff --name-only --diff-filter=U        # the conflicts
```

A merge keeps the fork's history as it is and resolves conflicts once. Do not
rebase the fork's commits onto upstream or force-push `main`.

## 3. Resolve conflicts: keep both sides

Almost every conflict is two features touching the same lines. Keep both.
Known hotspots and how they were settled:

| File | Rule |
|---|---|
| `CHANGELOG.md` | Take upstream's file, then put back the fork's own entries under `[Unreleased]`. Find them with `git diff -U0 $(git merge-base origin/main upstream/main) origin/main -- CHANGELOG.md`, which also shows which Added / Fixed / Security section each hunk sits in. |
| `apps/api/config.py` | Keep both sides' settings and constants. |
| `apps/api/main.py` | Keep upstream's middleware order. Add `SecurityHeadersMiddleware` last so it wraps every response, and don't register the rate-limit or setup-guard middleware twice. |
| `apps/api/routers/auth.py` | Keep the fork's per-address sign-in code limit (`reserve_magic_code_send` / `clear_magic_code_sends`, counted against `user.email`) next to upstream's logic. |
| `apps/api/tasks/celery_app.py` | `send_task_safe` must dispatch inline when `settings.running_on_vercel or settings.uses_vercel_queues`; otherwise use upstream's mechanism (currently a bounded `_dispatch_executor`). |
| `apps/web/stores/branding-store.ts` | Keep the fork's `cached` flag, `brandingFetchedAt` and `FETCH_TIMEOUT_MS`, which drive first-paint branding. Logo URLs are stable since v1.21.0, so add no expiry. |
| `apps/web/app/layout.tsx` | Keep the fork's Vercel Insights and noindex alongside upstream's changes. |
| Any web component | Upstream's layout, with the fork's readable accent text: on `bg-accent`, use `text-accent-fg` / `text-accent-fg-inverse` / `text-accent-fg-primary` instead of `text-white` / `text-text-inverse`. |
| A file upstream deleted that the fork edited | Accept the delete and carry the fork's edit to wherever upstream moved that code. |

For a file the fork changed only in small ways (colour classes, say), the
cleanest fix is often `git show upstream/main:<file> > <file>` followed by
reapplying the fork's diff from `git diff <merge-base> origin/main -- <file>`.

When both sides changed the same logic and keeping both is impossible, ask the
user before picking one.

## 4. Find what merged cleanly but still breaks

- **New upstream UI against fork rules.** Sweep files upstream added or changed for
  white or inverse text on the accent colour:
  `git diff --name-only <merge-base> upstream/main -- apps/web` and look for
  `bg-accent` near `text-white` / `text-text-inverse`. The fork's tests
  (`focus-outline-flash.test.tsx`, `accent-foreground.test.ts`) also catch
  new code that breaks its rules.
- **Fork tests that mock code upstream rewrote** (for example
  `test_vercel_deploy.py` patching the Celery dispatch). Update the test to
  the new mechanism; never delete or skip it.
- **Migrations:** there must be exactly one Alembic head. If both sides added
  a revision off the same parent, add a merge migration.
- **Vercel Sandbox:** `services/vercel_sandbox.py` forwards every Settings
  variable and `TRANSCODER_*` automatically. A new upstream transcoder
  setting read with `os.getenv` under another prefix needs adding there.

## 5. Run the CI checks locally

CI (`.github/workflows/ci.yml`) needs Postgres for some backend tests. In a
cloud session without Docker, start one with the system Postgres:

```bash
B=/usr/lib/postgresql/16/bin; D=/var/tmp/ffpg; mkdir -p $D && chown postgres $D
su postgres -c "$B/initdb -D $D/data -U postgres -A trust && $B/pg_ctl -D $D/data -l $D/log -o '-p 5432 -k /tmp' start"
psql -h localhost -U postgres -c "CREATE USER \"user\" PASSWORD 'pass' SUPERUSER;" -c "CREATE DATABASE freeframe_test OWNER \"user\";"
```

Then, with CI's env vars (`DATABASE_URL=postgresql://user:pass@localhost:5432/freeframe_test`,
`JWT_SECRET`, `S3_*` and the rest from `ci.yml`):

```bash
(cd apps/api && alembic upgrade head)
python -m pytest apps/api/tests/ -q
pnpm --filter web exec tsc --noEmit
pnpm --filter web lint
pnpm --filter web test        # run alone; perf tests can time out under parallel load
pnpm --filter web build
```

All must pass before pushing.

## 6. Commit, push, PR

- Commit the merge as `chore: merge upstream Techiebutler/freeframe main (vX.Y.Z) into the fork`,
  listing each conflict and how it was resolved.
- Open a PR into the fork's `main`. Its body follows `.github/PULL_REQUEST_TEMPLATE.md`.
- Merge with **"Create a merge commit"**, never Squash or Rebase. Only a merge
  commit records upstream's commits as included, which is what clears GitHub's
  "behind" count.

## 7. Verify production

- `vercel.json` deploys only `main`, so the sync branch gets no preview. After
  merging, check the merge commit's `Vercel` commit status reads "Deployment
  has completed".
- FreeFrame reports no version number (the API says `1.0.0`). Confirm the
  release by checking `git merge-base --is-ancestor <release tag> origin/main`,
  and ask the user to look for a feature new in that release on the live site.
- Afterwards, `git rev-list --left-right --count origin/main...upstream/main`
  should end in `0`.
