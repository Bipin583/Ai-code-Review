# 🤖 ReviewBot AI

AI-powered code review for GitHub pull requests. Open a PR, and ReviewBot posts a
summary comment plus inline comments on the exact lines that need attention.

Built with FastAPI, PyGithub and the Anthropic Messages API (via AgentRouter), with a Streamlit dashboard
for metrics.

## Documentation

Full documentation lives in [`docs/`](docs/README.md).

| Document | What it covers |
| --- | --- |
| [Setup](docs/setup.md) | End-to-end setup: credentials, `.env`, webhook, first review |
| [Configuration](docs/configuration.md) | Every environment variable; switching model providers |
| [Architecture](docs/architecture.md) | Components, request lifecycle, failure isolation, design decisions |
| [Review pipeline](docs/review-pipeline.md) | How one review works, from diff to posted comment |
| [API reference](docs/api-reference.md) | Every endpoint with request/response examples |
| [Database](docs/database.md) | Schema, JSON payload shapes, queries, PostgreSQL |
| [Deployment](docs/deployment.md) | Docker, Railway, Render, production hardening checklist |
| [Development](docs/development.md) | Layout, tests, and how to extend the bot |
| [Troubleshooting](docs/troubleshooting.md) | Symptom → cause → fix |

## Features

- **Automatic PR reviews** — GitHub webhook on `opened`, `synchronize`, `reopened`
  and `ready_for_review`
- **Incremental reviews** — a new push reviews only the delta since the last
  reviewed commit, with a safe fallback to a full review
- **Walkthrough** — a plain-English "what this PR does" paragraph at the top of
  every summary
- **Per-repo config** — a `.reviewbot.yaml` in the repository controls path
  filters, extensions, the inline-comment cap and the severity threshold
- **Bug detection** — logic errors, edge cases, crash risks
- **Security scanning** — injection, hardcoded secrets, unsafe operations
- **Code quality** — smells, complexity, duplication, naming
- **Performance notes** — inefficient loops, needless work
- **Best practices** — PEP 8, type hints, docstrings, imports
- **Inline comments** on real, commentable diff lines (no 422s from GitHub)
- **Confidence scores** per review, stored and charted
- **Dashboard** — Streamlit + Plotly over the API
- **Guardrails** — per-PR file cap, diff size cap, inline comment cap

## How it works

```
GitHub PR event
      │
      ▼
POST /webhook/github ──► HMAC-SHA256 signature check
      │
      ▼
Background task
      │
      ├─► PyGithub: read .reviewbot.yaml at the PR base sha
      ├─► incremental (synchronize)? compare with the last reviewed
      │   commit and keep only the delta — else list changed files
      ├─► keep reviewable files (extensions + include/exclude)
      ├─► annotate each patch with real new-file line numbers
      ├─► LLM (Anthropic Messages API): JSON findings per file
      ├─► LLM: walkthrough paragraph for the PR
      ├─► SQLite/PostgreSQL: store review + rendered comments
      ├─► PR summary comment
      └─► inline comments on the changed lines
```

Diffs are annotated with the line numbers of the *new* file before they reach the
model, and every returned line number is validated against the set of lines GitHub
will actually accept a comment on. Findings on lines outside the diff still appear in
the summary comment, so nothing is dropped silently.

A repository can override part of the review behaviour by committing a
`.reviewbot.yaml` (path filters, extensions, inline-comment cap, severity threshold,
on/off switch). It is read at the PR base sha, so a pull request cannot weaken its
own review. See [docs/configuration.md](docs/configuration.md).

## Quick start

The abbreviated version. [docs/setup.md](docs/setup.md) has the same path with every
detail, including how to create each credential and how to expose localhost to GitHub.

### 1. Install

```bash
python -m pip install -r requirements.txt
python -m pip install -e .
```

Python 3.10+ is required.

### 2. Configure

```bash
cp .env.example .env
```

Then fill in `.env`:

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `GITHUB_TOKEN` | yes | — | PAT with `repo` scope (or a GitHub App token) |
| `GITHUB_WEBHOOK_SECRET` | yes in prod | — | Shared secret for webhook signatures |
| `ANTHROPIC_AUTH_TOKEN` | yes | — | Anthropic-compatible bearer token |
| `ANTHROPIC_BASE_URL` | no | `https://api.anthropic.com` | Anthropic Messages API endpoint |
| `ANTHROPIC_MODEL` | no | `gpt-5.6-sol` | Provider model identifier |
| `LLM_TEMPERATURE` | no | `0.2` | Sampling temperature |
| `LLM_MAX_TOKENS` | no | `2000` | Response cap per file |
| `LLM_MAX_RETRIES` | no | `2` | Retries per file on API errors |
| `MAX_FILES_PER_REVIEW` | no | `0` | Eligible files reviewed per PR; `0` means unlimited |
| `MAX_DIFF_CHARS` | no | `12000` | Diff characters sent per file |
| `MAX_INLINE_COMMENTS` | no | `25` | Inline comments posted per PR |
| `REVIEW_FILE_EXTENSIONS` | no | `.py` | Comma-separated suffixes to review |
| `DATABASE_URL` | no | `sqlite:///./data/reviewbot.db` | SQLite or PostgreSQL |
| `APP_ENV` | no | `development` | `production` enables strict webhook checks |
| `LOG_LEVEL` | no | `INFO` | Python log level |
| `CORS_ORIGINS` | no | `*` | Comma-separated origins for the dashboard |

### 3. Run the API

```bash
uvicorn reviewbot.api.main:app --reload
```

- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

### 4. Point GitHub at it

Expose the port (for local development):

```bash
ngrok http 8000
```

In your repository: **Settings → Webhooks → Add webhook**

- Payload URL: `https://<your-host>/webhook/github`
- Content type: `application/json`
- Secret: the same value as `GITHUB_WEBHOOK_SECRET`
- Events: **Pull requests**

### 5. Run the dashboard

```bash
streamlit run dashboard/app.py
```

It reads from the API at `REVIEWBOT_API_URL` (default `http://localhost:8000`).

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Service banner |
| `GET` | `/health` | Liveness, database state, which secrets are configured |
| `POST` | `/webhook/github` | GitHub webhook receiver (signature required) |
| `GET` | `/api/reviews` | Recent reviews (`limit`, `offset`, `repo`) |
| `GET` | `/api/reviews/{id}` | One review with issues and comments |
| `GET` | `/api/reviews/{id}/comments` | Inline comments for a review |
| `GET` | `/api/repos` | Repositories seen, with counts |
| `GET` | `/api/metrics` | Aggregate metrics for the dashboard |
| `POST` | `/api/review` | Manually queue a review |

Full request and response shapes: [docs/api-reference.md](docs/api-reference.md).

Manual review:

```bash
curl -X POST http://localhost:8000/api/review \
  -H "Content-Type: application/json" \
  -H "X-ReviewBot-Token: $GITHUB_WEBHOOK_SECRET" \
  -d '{"repo": "owner/repo", "pr_number": 42}'
```

> **Note on access control.** The read endpoints are unauthenticated — they are
> intended to sit behind your own network boundary or an auth proxy, not on the open
> internet. `POST /api/review` makes the bot write to GitHub, so it requires
> `X-ReviewBot-Token` to match `GITHUB_WEBHOOK_SECRET`; when no secret is set it is
> refused outright in production and allowed (with a warning) in development.
> `POST /webhook/github` always verifies GitHub's HMAC signature, and refuses every
> request when `APP_ENV=production` and no secret is configured.

## Testing

```bash
pytest                                        # all tests, no API keys needed
pytest --cov=reviewbot --cov-report=term-missing
REVIEWBOT_LIVE_TESTS=1 pytest -k real_api     # opt-in live model call
```

The suite fakes GitHub and the model, and runs against a throwaway SQLite database in
a temp directory, so it is safe to run anywhere. See
[docs/development.md](docs/development.md) for the fixtures and how to add tests.

## Deployment

Summarised below; [docs/deployment.md](docs/deployment.md) adds the production
hardening checklist.

### Docker

```bash
docker build -t reviewbot-ai .
docker run -p 8000:8000 --env-file .env reviewbot-ai
```

The image binds `$PORT` (default 8000) and runs as a non-root user, so it works on
Railway and Render as-is.

### Railway / Render

1. Push the repository to GitHub
2. Create a new service from the repo (Dockerfile is detected automatically)
3. Set the environment variables from the table above
4. For anything beyond a single instance, set `DATABASE_URL` to a PostgreSQL URL —
   SQLite lives on the container filesystem and will not survive a redeploy

## Project structure

```
src/reviewbot/
├── api/          FastAPI app: webhook receiver, read APIs, background pipeline
├── github/       PyGithub wrapper and payload models
├── llm/          diff annotation, prompting, response parsing, comment rendering
├── db/           SQLAlchemy engine, session and models
├── dashboard/    Streamlit UI
└── utils/        settings + per-repo .reviewbot.yaml loading
configs/          re-export shim: `from configs.settings import settings`
dashboard/app.py  launcher for `streamlit run`
tests/            pytest suite
```

## Notes

- The model is reached through an Anthropic Messages-compatible endpoint. Set
  `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_MODEL` to match the
  selected provider.
- Reviews are stored per head commit, so a `synchronize` event produces a new review
  row rather than overwriting the previous one.
- `data/` and `models/` are kept in the repository via `.gitkeep`; the SQLite file
  inside `data/` is gitignored.

## License

MIT
