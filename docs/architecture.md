# Architecture

## System at a glance

```
                    ┌──────────────────────────────────────────┐
   GitHub PR event   │            FastAPI application           │
  ────────────────►  │                                          │
  X-Hub-Signature-256│  POST /webhook/github                    │
                     │    ├─ verify HMAC-SHA256 over raw body   │
                     │    ├─ filter event / action / draft      │
                     │    └─ 202 + BackgroundTasks.add_task ────┼──┐
                     │                                          │  │
                     │  GET  /api/reviews, /api/metrics, …      │  │
                     │  POST /api/review  (X-ReviewBot-Token)   │  │
                     └──────────────────────────────────────────┘  │
                                                                   │
        ┌──────────────────────────────────────────────────────────┘
        ▼  process_pr_review()  (background thread)
   ┌─────────────┐   1. changed files    ┌──────────────┐
   │ GitHubClient│ ◄────────────────────►│  GitHub API  │
   └─────────────┘   5. post comments    └──────────────┘
        │
        │ 2. annotated diff per file
        ▼
   ┌─────────────┐  Anthropic Messages   ┌──────────────┐
   │ CodeReviewer│ ◄────────────────────►│ Configured   │
   └─────────────┘   JSON response       │ LLM gateway  │
        │                                │ ANTHROPIC_   │
        │ 3. normalized findings         │ BASE_URL     │
        ▼
   ┌─────────────┐   4. persist          ┌──────────────┐
   │  SQLAlchemy │ ─────────────────────►│ SQLite /     │
   └─────────────┘                       │ PostgreSQL   │
                                         └──────┬───────┘
                                                │ read-only
                                         ┌──────▼───────┐
                                         │  Streamlit   │
                                         │  dashboard   │
                                         └──────────────┘
```

## Components

| Module | Responsibility | Entry points |
| --- | --- | --- |
| `api/main.py` | App assembly, CORS, lifespan, `/` and `/health` | `app`, `lifespan`, `health` |
| `api/webhooks.py` | Signature verification, event filtering, the review pipeline | `github_webhook`, `process_pr_review` |
| `api/routes.py` | Dashboard read APIs and the manual trigger | `list_reviews`, `get_metrics`, `trigger_review` |
| `github/client.py` | Thin PyGithub wrapper: read PR files, compare commits, fetch repo config, post comments | `GitHubClient.get_pr_files`, `compare_commits`, `get_contents`, `post_inline_comments` |
| `github/models.py` | Pydantic views of GitHub payloads | `GitHubFile`, `GitHubPR`, `ComparisonResult`, `WebhookPayload` |
| `llm/reviewer.py` | Prompting, retries, response normalization, aggregation, walkthrough | `CodeReviewer.review_file`, `review_multiple_files`, `_generate_walkthrough` |
| `llm/parser.py` | Diff annotation and GitHub comment rendering | `annotate_diff`, `format_inline_comments` |
| `db/models.py` | ORM models | `Review`, `ReviewComment` |
| `db/database.py` | Engine, session factory, schema bootstrap + add-column migration | `engine`, `SessionLocal`, `init_db` |
| `utils/config.py` | Single source of configuration truth | `Settings`, `settings` |
| `utils/repo_config.py` | Per-repo `.reviewbot.yaml`: parse, validate, cache, merge over globals | `RepoConfig`, `EffectiveReviewConfig`, `get_repo_config` |
| `dashboard/app.py` | Streamlit UI over the read APIs | `main` |

## Request lifecycle

A PR event must be answered fast — GitHub gives a webhook about 10 seconds before
it records a delivery failure — but a review takes far longer than that. So the
endpoint validates and enqueues, then returns immediately.

1. **Receive** — `github_webhook` reads the raw body with `await request.body()`.
   The raw bytes matter: the HMAC is computed over exactly what GitHub sent, so the
   body cannot be re-serialized from a parsed model first.
2. **Verify** — `verify_signature` compares `X-Hub-Signature-256` against
   `hmac.new(secret, body, sha256)` using `hmac.compare_digest`. Invalid → `401`.
3. **Filter** — non-`pull_request` events are ignored; actions outside
   `{opened, synchronize, reopened, ready_for_review}` are ignored; draft PRs are
   ignored unless the action is `ready_for_review`. Each returns `200` with a
   `{"status": "ignored", "reason": …}` body so GitHub's delivery log stays green.
4. **Enqueue** — `background_tasks.add_task(process_pr_review, …)` and respond
   `202 {"status": "accepted"}`.
5. **Review** (background thread) — see [review-pipeline.md](review-pipeline.md).

`POST /api/review` joins the same pipeline at step 4, which is why a manual review
behaves identically to a webhook-triggered one.

## Failure isolation

A review touches two external services and an LLM that can return anything. Three
independent layers keep one bad input from taking anything else down:

| Layer | Guarantee | Where |
| --- | --- | --- |
| Per file | `review_file` never raises — errors become an empty review whose `summary` explains what happened | `llm/reviewer.py` |
| Per PR | `process_pr_review` catches everything and logs a traceback; the worker survives | `api/webhooks.py` |
| Per comment | `post_inline_comments` logs and skips individual failures, returning how many GitHub accepted | `github/client.py` |

The practical effect: one unparseable model response degrades a single file's
review, one stale line number loses a single comment, and neither aborts the PR.

## Design decisions

**Diffs are annotated with real line numbers.** A model cannot reliably infer
new-file line numbers from a bare unified diff, and a wrong number makes GitHub
reject the inline comment with a 422. `annotate_diff` prefixes every line with its
number in the new file and returns the set of lines that are commentable, which is
then used to validate whatever the model reports.

**Issue lists are flat, not per-file.** Each issue dict carries the `file` it came
from. Nesting lists per file loses the filename once the lists are concatenated for
storage, and the flat shape sorts globally by severity for free.

**Clients are constructed lazily.** `GitHubClient.gh` and `CodeReviewer.client` are
properties that build on first use, so the app imports and `/health` answers with
no credentials configured. Only the code paths that need a secret fail.

**Comments are two-tier.** One conversation-level comment carries the summary,
breakdown table and recommendations; inline comments carry individual findings.
Findings whose line is not commentable are dropped from the inline set but remain
in the summary, so nothing is silently discarded.

**Incremental reviews trust the database, not the payload.** On `synchronize` the
delta starts at the most recent stored `Review.commit_sha`, not the webhook's
`before` field: a lost webhook would leave `before` pointing at a commit that was
never reviewed, and the delta would silently skip it. The database can only point
at commits the bot actually reviewed. When the delta cannot be computed safely —
compare failure, or `behind_by > 0` after a force-push — the pipeline falls back to
a full review rather than reviewing a wrong diff.

**Repo config is read at the PR base sha.** `.reviewbot.yaml` is fetched through
`GitHubClient.get_contents` with the PR's base ref, cached per `(repo, ref)`, and
merged over the global settings as an `EffectiveReviewConfig` that is threaded
through file selection, the inline-comment gate, and the reviewer. Reading it at
the base means a PR cannot weaken its own review by shipping an `enabled: false`
config. `max_files_per_review` and `max_diff_chars` stay global-only — they are
the operator's cost guardrails.

**The walkthrough is a separate, optional call.** Extending the per-file JSON
schema would couple the walkthrough to a response that already occasionally fails
to parse. Instead one lightweight plain-text call runs after aggregation, using
only the PR title and per-file finding counts, and any failure just omits the
section.

**The provider is configuration, not code.** The reviewer uses the Anthropic Messages
API at `ANTHROPIC_BASE_URL`, so switching to a compatible gateway is an `.env` edit.
See [configuration.md](configuration.md).

**Timestamps are naive UTC.** `utcnow()` returns `datetime.now(timezone.utc)` with
the tzinfo stripped — `datetime.utcnow` is deprecated in 3.12+, and naive columns
keep SQLite and PostgreSQL behaving identically.

**Findings are stored as JSON columns.** Issue shapes evolve with the prompt; JSON
columns absorb that without a migration, and every read path tolerates missing keys.
