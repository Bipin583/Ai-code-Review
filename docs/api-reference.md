# API reference

Base URL in development: `http://localhost:8000`. Interactive OpenAPI docs are
served at `/docs`, the raw schema at `/openapi.json`.

## Authentication summary

| Endpoint | Auth |
| --- | --- |
| `GET /`, `GET /health` | none |
| `GET /api/reviews`, `/api/reviews/{id}`, `/api/reviews/{id}/comments`, `/api/repos`, `/api/metrics` | **none** — read-only, intended to sit behind your own network boundary or auth proxy |
| `POST /api/review` | `X-ReviewBot-Token` must equal `GITHUB_WEBHOOK_SECRET` |
| `POST /webhook/github` | `X-Hub-Signature-256` HMAC-SHA256 over the raw body |

The read endpoints expose repository names, file paths, code excerpts inside
findings, and commit SHAs. Do not put them on the open internet unprotected.

## Meta

### `GET /`

Service banner and an endpoint index.

```json
{
  "name": "ReviewBot AI",
  "version": "0.1.0",
  "status": "running",
  "docs": "/docs",
  "endpoints": {
    "webhook": "POST /webhook/github",
    "reviews": "GET /api/reviews",
    "metrics": "GET /api/metrics",
    "manual_review": "POST /api/review"
  }
}
```

### `GET /health`

Liveness probe. Executes `SELECT 1` and reports which credentials are configured —
booleans only, never values.

```json
{
  "status": "healthy",
  "version": "0.1.0",
  "environment": "development",
  "database": "ok",
  "database_backend": "sqlite",
  "model": "gpt-5.6-sol",
  "config": {"github_token": true, "webhook_secret": true, "anthropic_auth_token": true}
}
```

`status` becomes `degraded` and `database` becomes `error` if the query fails; the
endpoint still returns `200` so a proxy can distinguish "process alive, database
down" from "process gone". Container health checks use this path.

## Webhook

### `POST /webhook/github`

The endpoint GitHub calls. Always returns quickly — the review itself runs in a
background task — because GitHub abandons a webhook delivery after roughly ten
seconds.

**Headers**

| Header | Required | Notes |
| --- | --- | --- |
| `X-Hub-Signature-256` | yes in production | `sha256=<hex>` HMAC of the raw body using `GITHUB_WEBHOOK_SECRET` |
| `X-GitHub-Event` | yes | Only `pull_request` and `ping` do anything |
| `Content-Type` | yes | Must be `application/json`; GitHub's `form-urlencoded` option will not parse |

The signature is computed over the **raw request bytes**, so the handler reads
`await request.body()` before any JSON parsing — re-serializing the payload would
change the byte sequence and break verification.

**Responses**

| Body | When |
| --- | --- |
| `{"status": "accepted", "repo": ..., "pr_number": ..., "action": ...}` | Review queued |
| `{"status": "pong", "zen": "..."}` | `ping` event — GitHub sends this when you first save the hook |
| `{"status": "ignored", "reason": "event push not handled"}` | Any event other than `pull_request` |
| `{"status": "ignored", "reason": "action closed not handled"}` | Action outside `opened`, `synchronize`, `reopened`, `ready_for_review` |
| `{"status": "ignored", "reason": "draft pull request"}` | PR is a draft and the action was not `ready_for_review` |
| `401 Invalid signature` | HMAC mismatch, or missing signature while `APP_ENV=production` |
| `400 Body is not valid JSON` | Malformed payload |
| `400 Missing repository or PR number` | Payload shape unexpected |

Every non-`accepted` outcome is a `200` with a reason, not an error. GitHub's
delivery log therefore shows a green tick for events the bot deliberately skipped,
which keeps real failures visible.

`synchronize` fires on every push to an open PR, so a branch pushed ten times gets
ten reviews. `MAX_FILES_PER_REVIEW=0` reviews every eligible file; set a positive
value to bound model calls. `MAX_INLINE_COMMENTS` independently bounds comments.

## Reviews

### `GET /api/reviews`

Most recent first, ordered by `created_at DESC, id DESC`.

| Query param | Default | Constraint |
| --- | --- | --- |
| `limit` | 20 | 1–200 |
| `offset` | 0 | ≥ 0 |
| `repo` | none | exact `owner/repo` match |

```json
{
  "total": 42,
  "limit": 20,
  "offset": 0,
  "reviews": [
    {
      "id": 42,
      "repo_name": "acme/api",
      "pr_number": 137,
      "commit_sha": "9f2c1ab...",
      "summary": "## 🤖 AI Code Review\n...",
      "confidence_score": 0.86,
      "files_reviewed": 3,
      "created_at": "2026-09-03T16:04:11.512000",
      "updated_at": "2026-09-03T16:05:47.880000",
      "issue_counts": {"bugs": 2, "security": 1, "smells": 4, "performance": 0, "best_practices": 3},
      "total_issues": 10
    }
  ]
}
```

`total` is the count **before** pagination, so `total > offset + len(reviews)` means
there is another page. The list view omits the issue bodies; fetch one review to get
them. Timestamps are naive UTC ISO-8601 — no `Z`, no offset.

### `GET /api/reviews/{id}`

Same object plus `issues` (the five arrays in full) and `comments` (every rendered
inline comment, ordered by file path then line number). `404` if the id is unknown.

```json
{
  "id": 42,
  "issues": {
    "bugs": [
      {
        "line": 88,
        "description": "fetchone() returns None when no row matches, so [0] raises TypeError.",
        "severity": "high",
        "suggestion": "row = cur.fetchone()\nif row is None:\n    return default",
        "file": "src/store.py"
      }
    ],
    "security": [], "smells": [], "performance": [], "best_practices": []
  },
  "comments": [
    {
      "id": 301,
      "review_id": 42,
      "file_path": "src/store.py",
      "line_number": 88,
      "comment": "### 🐛 Bug — High\n...",
      "comment_type": "bugs",
      "severity": "high",
      "confidence": 0.86,
      "posted": true,
      "created_at": "2026-09-03T16:05:47.880000"
    }
  ]
}
```

`posted` is the honest record of whether GitHub accepted that inline comment. A
`false` with a populated row means the comment was generated and stored but rejected
or unreachable — usually a line GitHub would not accept, or a permissions problem.

### `GET /api/reviews/{id}/comments`

Just the comments, when you do not want the issue payloads: `{"review_id": 42,
"count": 7, "comments": [...]}`. `404` if the review does not exist.

## `GET /api/repos`

Repositories seen so far, busiest first.

```json
{"repos": [{"repo_name": "acme/api", "reviews": 31, "last_review": "2026-09-03T16:04:11.512000"}]}
```

## `GET /api/metrics`

Everything the dashboard's headline numbers are built from, in one call.

```json
{
  "total_reviews": 42,
  "total_issues": 318,
  "total_bugs": 51,
  "total_security": 12,
  "total_smells": 140,
  "total_performance": 27,
  "total_best_practices": 88,
  "severity_breakdown": {"high": 34, "medium": 171, "low": 113},
  "average_confidence": 0.84,
  "total_files_reviewed": 96,
  "total_comments": 240,
  "posted_comments": 231,
  "repos_reviewed": 4,
  "reviews_last_7_days": 9,
  "issues_per_review": 7.57
}
```

`posted_comments` versus `total_comments` is the health signal worth watching: a
widening gap means GitHub is rejecting inline comments. `severity_breakdown` counts
any unrecognised severity as `medium`, matching how issues are normalized on the way
in. This endpoint loads every review row to aggregate in Python — fine for thousands
of reviews, worth replacing with SQL aggregates if you reach a much larger scale.

## `POST /api/review`

Manually queue a review — the same pipeline the webhook triggers, useful for testing
without pushing, or for re-reviewing after changing a prompt or model.

```bash
curl -X POST http://localhost:8000/api/review \
  -H 'Content-Type: application/json' \
  -H "X-ReviewBot-Token: $GITHUB_WEBHOOK_SECRET" \
  -d '{"repo": "owner/repo", "pr_number": 1}'
```

| Field | Required | Notes |
| --- | --- | --- |
| `repo` | yes | Must contain `/`, else `400 repo must be owner/repo` |
| `pr_number` | yes | Integer ≥ 1 |
| `commit_sha` | no | Defaults to the PR head SHA fetched from GitHub |

Returns `202 {"status": "accepted", "repo": ..., "pr_number": ...}`. It comments on a
real pull request, so the token check is not optional:

- Secret configured → `X-ReviewBot-Token` must match it, compared with
  `hmac.compare_digest`, or `401`.
- No secret, `APP_ENV=development` → allowed, with a warning logged.
- No secret, `APP_ENV=production` → `503`, the endpoint is disabled rather than
  left open.

Because the work is queued, `202` means "accepted", not "succeeded". The outcome
shows up in the logs and in `GET /api/reviews`.

## Errors

FastAPI's standard shape throughout:

```json
{"detail": "Review not found"}
```

Validation failures return `422` with per-field detail — for example `pr_number: 0`
reports `"Input should be greater than or equal to 1"`.
