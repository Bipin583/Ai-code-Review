# Deployment

The webhook receiver has to be reachable from GitHub over HTTPS. The dashboard does
not, and should not be.

## What you are deploying

Two processes from one codebase:

| Process | Command | Public? |
| --- | --- | --- |
| API + webhook receiver | `uvicorn reviewbot.api.main:app --host 0.0.0.0 --port $PORT` | yes, HTTPS |
| Dashboard | `streamlit run dashboard/app.py` | no — keep it private |

They share a database. On SQLite that means the same filesystem, which is the main
reason to switch to PostgreSQL as soon as the two run on separate hosts.

## Docker

```bash
docker build -t reviewbot-ai .
docker run --rm -p 8000:8000 --env-file .env \
  -v "$(pwd)/data:/app/data" reviewbot-ai
```

The image is `python:3.11-slim`, installs requirements as their own layer so source
edits do not reinstall dependencies, runs as the non-root user `reviewbot` (uid
10001), and binds to `$PORT` when the platform sets one — which is what makes it work
unchanged on Railway and Render. A `HEALTHCHECK` polls `/health` every 30s.

Two things to get right:

- **Persist the database.** The default `DATABASE_URL` is already
  `sqlite:///./data/reviewbot.db`, so mounting a volume at `/app/data` is enough —
  or switch to PostgreSQL. Without one of those, every review disappears when the
  container is replaced.
- **Do not bake `.env` into the image.** `.dockerignore` already excludes `.env` and
  `data/*.db`; pass secrets with `--env-file` or your platform's secret store.

## Railway

1. Push the repository to GitHub and create a project from it. Railway detects the
   Dockerfile; no build configuration needed.
2. Add the variables from [configuration.md](configuration.md) — `GITHUB_TOKEN`,
   `GITHUB_WEBHOOK_SECRET`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
   `ANTHROPIC_MODEL`, and `APP_ENV=production`. Do not set `PORT`; Railway injects it.
3. Add a PostgreSQL database and set `DATABASE_URL` to its connection string, or
   attach a volume at `/app/data` and keep the default SQLite path.
4. Generate a domain, then point the GitHub webhook at
   `https://<your-app>.up.railway.app/webhook/github`.

## Render

Create a **Web Service** from the repository with runtime Docker. Health check path
`/health`. Add the same environment variables, and either a Render PostgreSQL
instance or a disk mounted at `/app/data`. The webhook URL is
`https://<your-service>.onrender.com/webhook/github`.

Render's free tier sleeps when idle. A cold start can exceed GitHub's webhook
timeout, so the first PR after a quiet period may show a failed delivery — redeliver
it from the repository's webhook page, or use a paid instance for anything real.

## Production hardening checklist

Work through this before pointing the bot at a repository that matters.

**Secrets**

- [ ] `APP_ENV=production` — this is what makes unsigned webhooks return `401`
      instead of being accepted with a warning, and what disables `POST /api/review`
      if no secret is set. Nothing else turns those protections on.
- [ ] `GITHUB_WEBHOOK_SECRET` set, and the identical value pasted into the GitHub
      webhook form.
- [ ] Credentials come from the platform's secret store, not a committed file. `.env`
      is in `.gitignore` and `.dockerignore`; keep it that way.
- [ ] Any token that has ever appeared in a terminal, a chat window or a log has been
      rotated.

**Exposure**

- [ ] Only `/webhook/github` needs to be publicly reachable. The read endpoints
      (`/api/reviews`, `/api/metrics`, …) have **no authentication** — they leak
      repository names, file paths and code excerpts from findings. Put them behind an
      auth proxy, an IP allowlist, or a private network.
- [ ] The dashboard is not on a public URL. It reads the same data with no login.
- [ ] `CORS_ORIGINS` lists your real origins. The default `*` is a development
      convenience.
- [ ] HTTPS only. GitHub will deliver to plain HTTP, but the payload includes your
      source diff.

**Blast radius**

- [ ] The GitHub token is scoped to the repositories the bot should touch, with *Pull
      requests: write* and *Contents: read* and nothing more. A classic PAT with `repo`
      grants far more than this bot needs.
- [ ] `MAX_FILES_PER_REVIEW`, `MAX_DIFF_CHARS` and `MAX_INLINE_COMMENTS` match values
      you have costed. `MAX_FILES_PER_REVIEW=0` is unlimited. Remember `synchronize`:
      one review per push, not per PR.
- [ ] You know where your code is going. Every reviewed diff is sent to whatever
      `ANTHROPIC_BASE_URL` points at, and that provider can log it.

**Operations**

- [ ] Database persists across restarts — PostgreSQL, or a mounted volume.
- [ ] Health check wired to `/health`. Note it returns `200` even when
      `status: degraded`, so alert on the body, not just the code.
- [ ] `LOG_LEVEL=INFO`. Logs are the only record of why a review was skipped.
- [ ] Watch `posted_comments` against `total_comments` in `/api/metrics`; a growing
      gap means GitHub is rejecting inline comments.

## Verifying a deployment

```bash
curl -s https://your-app/health
```

`database: "ok"` and all three `config` booleans `true`. Those booleans report that a
value is **present**, not that it works — a revoked token still reports `true`. Then
open the repository's webhook page: the `ping` delivery should show `200` with
`{"status": "pong"}`, and the first real PR should show `{"status": "accepted"}`.

If a delivery shows `401`, the secret in GitHub and the secret in the environment do
not match. Re-paste both; do not retype them.
