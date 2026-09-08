# Setup

From an empty checkout to a bot commenting on a real pull request.

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Python 3.10+ | Tested on 3.10, 3.11 and 3.12 in CI |
| A GitHub repository | You need admin rights on it to add a webhook |
| An LLM API token | AgentRouter, Anthropic, or an Anthropic Messages-compatible endpoint |
| A public URL | Only for *automatic* reviews — a tunnel in development, a host in production |

## 1. Install

```bash
pip install -r requirements.txt
pip install -e .
```

The editable install is what makes `import reviewbot` work from anywhere. Without
it you must run everything from the repository root, where `conftest.py` and
`pyproject.toml`'s `pythonpath` put `src/` on the path.

## 2. Create the credentials

### GITHUB_TOKEN

A fine-grained personal access token at
**github.com/settings/personal-access-tokens** → *Generate new token*.

- **Resource owner:** the account or organization owning the repo
- **Repository access:** *Only select repositories* → pick the repos to review
- **Permissions:**
  - `Pull requests: Read and write` — **required**, this is how comments are posted
  - `Contents: Read-only` — read file contents
  - `Metadata: Read-only` — granted automatically
  - `Administration: Read and write` — optional, only if you want to create the
    webhook through the API instead of the web UI

A classic token with the `repo` scope also works. Comments appear under whichever
account owns the token, so a dedicated bot account is nicer for team repos.

### GITHUB_WEBHOOK_SECRET

Nobody issues this one — you invent it, then give the same string to GitHub:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

It does double duty as the credential for `POST /api/review`, sent as the
`X-ReviewBot-Token` header.

### ANTHROPIC_AUTH_TOKEN

Create a token with your Anthropic Messages-compatible provider. For AgentRouter,
use the token and base URL from its setup instructions. Whatever endpoint you configure
receives every diff the bot reviews, which is your source code.

## 3. Fill in `.env`

```bash
cp .env.example .env
```

Then set the three values you just created:

```ini
GITHUB_TOKEN=github_pat_...
GITHUB_WEBHOOK_SECRET=...64 hex chars...
ANTHROPIC_AUTH_TOKEN=your-provider-token
ANTHROPIC_BASE_URL=https://agentrouter.org
ANTHROPIC_MODEL=gpt-5.6-sol
```

Everything else in the file has a working default — see
[configuration.md](configuration.md).

## 4. Verify

```bash
uvicorn reviewbot.api.main:app --reload
```

Open `http://localhost:8000/health`:

```json
{
  "status": "healthy",
  "database": "ok",
  "model": "gpt-5.6-sol",
  "config": {"github_token": true, "webhook_secret": true, "anthropic_auth_token": true}
}
```

All three booleans must be `true`. They report *presence*, not validity — a typo'd
key still shows `true` and fails at call time. `/health` never returns credential
values.

Interactive API docs are at `http://localhost:8000/docs`.

## 5. Expose the app (automatic reviews only)

GitHub cannot reach `localhost`. In development, tunnel it:

```bash
ngrok http 8000                                   # https://xxxx.ngrok-free.app
cloudflared tunnel --url http://localhost:8000     # no account needed
```

In production, deploy instead — see [deployment.md](deployment.md).

## 6. Register the webhook

Repository → **Settings** → **Webhooks** → **Add webhook**:

| Field | Value |
| --- | --- |
| Payload URL | `https://<your-host>/webhook/github` |
| Content type | `application/json` — the default `form-urlencoded` will not parse |
| Secret | the same `GITHUB_WEBHOOK_SECRET` string |
| SSL verification | enabled |
| Events | *Let me select individual events* → **Pull requests** only |

GitHub immediately sends a `ping`. Under **Recent Deliveries** the top entry should
be `200` with `{"status": "pong"}`. A `401` means the secrets differ; a `404` means
the URL path is wrong.

## 7. Trigger the first review

Open a PR that changes a `.py` file. Within a minute or two the bot posts a summary
comment and inline comments on the changed lines.

To test without a webhook or a public URL, call the manual endpoint — it runs the
identical pipeline:

```bash
curl -X POST http://localhost:8000/api/review \
  -H "Content-Type: application/json" \
  -H "X-ReviewBot-Token: $GITHUB_WEBHOOK_SECRET" \
  -d '{"repo": "owner/repo", "pr_number": 1}'
```

It answers `202` immediately; watch the server log for progress. Reviews take
roughly 30–90 seconds per file depending on the provider.

## 8. Run the dashboard

```bash
streamlit run dashboard/app.py
```

It reads the API at `http://localhost:8000` by default; change the base URL in the
sidebar if the API runs elsewhere.
