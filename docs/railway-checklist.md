# Railway deploy checklist

Step-by-step for deploying ReviewBot from `Bipin583/Ai-code-Review` to Railway,
so PR reviews work without anything running locally. Full background:
[deployment.md](deployment.md).

## 1. Create the service

1. [railway.app](https://railway.app) → sign in with GitHub
2. **New Project → Deploy from GitHub repo** → `Bipin583/Ai-code-Review`
3. Railway detects the Dockerfile — accept the defaults

## 2. Set the variables

Service → **Variables** → add each of these:

```env
GITHUB_TOKEN=<your PAT>
GITHUB_WEBHOOK_SECRET=<same value as the GitHub webhook secret>
ANTHROPIC_AUTH_TOKEN=<AgentRouter token>
ANTHROPIC_BASE_URL=https://agentrouter.org
ANTHROPIC_MODEL=glm-5.3
LLM_MAX_TOKENS=16000
APP_ENV=production
```

⚠️ **`ANTHROPIC_MODEL` and `LLM_MAX_TOKENS` must be set explicitly.** Locally they
come from machine-specific settings; on Railway the defaults (`gpt-5.6-sol`,
`2000`) apply otherwise — and `2000` tokens is too few for a thinking model like
`glm-5.3`, which then returns empty responses on every file.

Do **not** set `PORT` — Railway injects it.

## 3. Add the database

1. Project → **New → Database → PostgreSQL**
2. Copy the connection string and set:

```env
DATABASE_URL=postgresql+psycopg2://<user>:<password>@<host>:5432/<db>
```

Without this, every review is lost each time the container is replaced.

## 4. Publish the URL

Service → **Settings → Networking → Generate Domain**, e.g.
`https://reviewbot-ai.up.railway.app`.

## 5. Point GitHub at it

Repo → **Settings → Webhooks** → edit the existing webhook:

| Field | Value |
| --- | --- |
| Payload URL | `https://<your-app>.up.railway.app/webhook/github` |
| Secret | unchanged |
| Events | unchanged (*Pull requests*) |

## 6. Verify

```bash
curl -s https://<your-app>.up.railway.app/health
```

- `status: "healthy"`, `database: "ok"`, all three `config` booleans `true`
- Webhook **Recent Deliveries**: the `ping` shows `200` with `{"status": "pong"}`
- Open a PR with a `.py` change → delivery shows `{"status": "accepted"}`, then the
  summary comment (with walkthrough) and inline comments appear

A `401` on delivery = the secret in Railway differs from the one in GitHub.

## 7. Shut down the local setup

Once the deploy verifies:

```bash
# stop uvicorn and the cloudflared tunnel
```

The `trycloudflare.com` URL is dead anyway after the tunnel stops.

## Security reminders

- `/api/reviews` and `/api/metrics` are **unauthenticated** on the public URL —
  they expose repo names, file paths and code excerpts. Keep the URL private or
  put it behind an auth proxy before sharing.
- `APP_ENV=production` is already set: unsigned webhooks are rejected, and
  `POST /api/review` requires `X-ReviewBot-Token`.
