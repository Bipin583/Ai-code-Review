# ReviewBot AI — Documentation

AI-powered code review for GitHub pull requests. A webhook receives PR events,
Claude reviews each changed file, and the findings are posted back to the PR as a
summary comment plus inline comments on the exact lines.

## Documentation map

| Document | What it covers |
| --- | --- |
| [setup.md](setup.md) | End-to-end setup from an empty checkout: credentials, `.env`, webhook, first review |
| [configuration.md](configuration.md) | Every environment variable, its default and effect; switching model providers |
| [architecture.md](architecture.md) | Components, request lifecycle, failure isolation, design decisions |
| [review-pipeline.md](review-pipeline.md) | How one review actually works, from diff to posted comment |
| [api-reference.md](api-reference.md) | Every HTTP endpoint with request/response examples |
| [database.md](database.md) | Schema, JSON payload shapes, useful queries, PostgreSQL migration |
| [deployment.md](deployment.md) | Docker, Railway, Render, production hardening checklist |
| [railway-checklist.md](railway-checklist.md) | Copy-along Railway quick start: exact variables, database, webhook cutover |
| [development.md](development.md) | Project layout, tests, and how to extend the bot |
| [troubleshooting.md](troubleshooting.md) | Symptom → cause → fix for the failures you are likely to hit |

## Where to start

- **Getting it running for the first time** → [setup.md](setup.md)
- **It runs but does nothing on my PR** → [troubleshooting.md](troubleshooting.md)
- **Understanding or modifying the review logic** → [review-pipeline.md](review-pipeline.md)
- **Shipping it somewhere permanent** → [deployment.md](deployment.md)

## The 60-second version

```bash
pip install -r requirements.txt && pip install -e .
cp .env.example .env                 # add GitHub credentials and ANTHROPIC_AUTH_TOKEN
uvicorn reviewbot.api.main:app --reload
```

Then either point a GitHub webhook at `POST /webhook/github` for automatic
reviews, or trigger one by hand:

```bash
curl -X POST http://localhost:8000/api/review \
  -H "Content-Type: application/json" \
  -H "X-ReviewBot-Token: $GITHUB_WEBHOOK_SECRET" \
  -d '{"repo": "owner/repo", "pr_number": 1}'
```

The dashboard (`streamlit run dashboard/app.py`) reads the same database and
shows metrics, trends and per-review detail.
