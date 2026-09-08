# Troubleshooting

Symptom → cause → fix, roughly in the order you are likely to hit them.

## Nothing happens when I open a PR

Work outwards from GitHub. Open the repository's webhook page →
**Recent Deliveries**. What you see there tells you where the problem is:

| Delivery shows | Meaning | Fix |
| --- | --- | --- |
| Nothing at all | GitHub never fired | The webhook does not exist, or is not subscribed to *Pull requests* |
| Red / connection error | GitHub cannot reach you | Tunnel or deployment is down; the URL must be HTTPS and end in `/webhook/github` |
| `401 Invalid signature` | Secret mismatch | Re-paste `GITHUB_WEBHOOK_SECRET` into both the environment and the GitHub form |
| `200 {"status": "ignored", ...}` | Received and deliberately skipped | Read the `reason` — see below |
| `200 {"status": "accepted", ...}` | The bot has it | The problem is downstream; check the application logs |

`ignored` reasons and what they mean:

- `event push not handled` — the webhook is subscribed to the wrong events. Only
  *Pull requests* is needed.
- `action closed not handled` — normal. Only `opened`, `synchronize`, `reopened` and
  `ready_for_review` trigger a review.
- `draft pull request` — mark it ready for review, and the `ready_for_review` event
  will start one.

## `accepted`, but no comments appear

The pipeline ran and decided there was nothing to do, or failed after accepting. The
logs say which. Common causes:

**No reviewable files.** Everything in the PR was deleted, binary, patch-less, or
outside `REVIEW_FILE_EXTENSIONS` (default `.py` only). The log line names the
extensions it was looking for. A PR touching only `.md` and `.yml` is correctly
skipped.

**Model call failing.** Look for `Error during review:` in the logs. The summary
comment, if one was posted, carries the same message. `401` means a bad or missing
API key; `404` usually means the model id does not exist at that provider; `429` is
rate limiting.

**`ANTHROPIC_AUTH_TOKEN is not set`.** The token is empty. Note that `/health`
reporting `anthropic_auth_token: true` only means a non-empty string is present — a
placeholder satisfies that check and then fails with `401` on every review. If
`/health` is green but reviews `401`, suspect a placeholder or revoked token.

**Cannot post.** `403 Resource not accessible by personal access token` means the
token lacks *Pull requests: write* for that repository. The review is still stored —
check `GET /api/reviews`.

## The summary posts but inline comments do not

Check `posted` in `GET /api/reviews/{id}/comments`. Rows with `posted: false` were
generated and stored but rejected by GitHub.

The usual cause is a line GitHub will not accept — it only allows comments on lines
present in the diff. `format_inline_comments` already filters against the set of
commentable lines, so a rejection here normally means the `commit_sha` is not the PR's
current head: pushing again while a review is running invalidates the SHA it started
with. Re-run the review.

If *every* comment failed, it is permissions (*Pull requests: write*) rather than line
numbers.

`total_comments` versus `posted_comments` in `GET /api/metrics` is the same signal
across all reviews.

## `401 Invalid signature` on every delivery

The HMAC is computed over the exact raw bytes GitHub sent, so:

- The secret must match byte for byte. Copy-paste both, never retype.
- The webhook's **Content type** must be `application/json`. With
  `application/x-www-form-urlencoded` the body is form-wrapped and both parsing and
  verification fail.
- Any proxy that re-serializes the body will break verification.

While `APP_ENV=development`, a missing signature is allowed with a logged warning so
you can `curl` the endpoint. In production it is a `401`.

## `POST /api/review` returns 401 or 503

- `401 Invalid or missing token` — send `X-ReviewBot-Token` with the value of
  `GITHUB_WEBHOOK_SECRET`.
- `503 Manual review is disabled` — you are in production with no secret set. Set
  `GITHUB_WEBHOOK_SECRET`; the endpoint writes to real pull requests and is not left
  open.
- `400 repo must be owner/repo` — `repo` needs the owner prefix.

## Reviews are slow, or expensive

One model call per eligible file, run sequentially. With the default
`MAX_FILES_PER_REVIEW=0`, a large PR can take many round trips. `synchronize` means a
review per push, not per PR — ten pushes to a branch is ten reviews.

Set `MAX_FILES_PER_REVIEW` to a positive cap, turn down `MAX_DIFF_CHARS` and
`LLM_MAX_TOKENS`, narrow
`REVIEW_FILE_EXTENSIONS`, or pick a faster model. If your provider injects a large
hidden system prompt, your input token count per call can be much higher than your
own prompt suggests — check `usage.prompt_tokens` on a small test call before
budgeting.

## `Error: the AI response was not valid JSON`

The model returned prose or a truncated object. Code fences are already stripped, so
this is usually one of:

- `LLM_MAX_TOKENS` too low, cutting the JSON off mid-object — raise it.
- A model that ignores JSON instructions — the request sends
  `response_format: json_object` when the provider supports it; if yours does not,
  choose a stronger model.
- `LLM_TEMPERATURE` raised well above the default `0.2` — bring it back down.

The file's findings are lost, the rest of the PR is unaffected.

## Findings point at the wrong lines

The model is meant to copy line numbers from the annotated diff. If comments land a
few lines off, check that `SYSTEM_PROMPT` still contains the line-number contract —
it is the only thing enforcing it — and that `annotate_diff` has not been modified.
Issues with an unusable line are stored with `line: null`: they appear in the summary
and the dashboard but cannot become inline comments, which is the intended fallback.

## Dashboard problems

**Empty dashboard.** It reads the database directly, so it is empty until a review has
run. Confirm with `GET /api/reviews`.

**Dashboard and API disagree.** Two different database files. Relative SQLite paths are
anchored to the project root precisely to prevent this, so a mismatch means one of the
two processes has a different `DATABASE_URL` — check both environments.

**`ModuleNotFoundError: reviewbot`.** Run `streamlit run dashboard/app.py` from the
project root; that launcher adds `src` to the path. Or `pip install -e .`.

## Database errors

**`SQLite objects created in a thread can only be used in that same thread`** — the
engine sets `check_same_thread=False` for SQLite. If you see this, something is
building its own engine or connection instead of using `db/database.py`.

**`no such column`** — you added a column to a model without changing the database.
`init_db` only creates missing *tables*; it never alters an existing one. Drop
`data/reviewbot.db` locally, or add Alembic. See [database.md](database.md).

**`unable to open database file`** in Docker — no volume mounted at `/app/data`, or it
is not writable by uid 10001.

## Windows

**`UnicodeEncodeError: 'charmap' codec can't encode character`** — the console is
cp1252 and the output contains emoji (the summary comment is full of them). Set
`PYTHONIOENCODING=utf-8` before running, or `chcp 65001`.

**`Scripts\activate` refused** — `Set-ExecutionPolicy -Scope Process RemoteSigned`, or
call `.venv\Scripts\python.exe` directly.

## Getting more detail

```env
LOG_LEVEL=DEBUG
```

Every decision the pipeline makes is logged: which files were kept and why others were
not, each retry with its backoff, how many comments were rendered and how many GitHub
accepted. `/health` tells you what the process thinks its configuration is.

If a review failed for reasons you cannot see, re-run just that PR:

```bash
curl -X POST http://localhost:8000/api/review \
  -H 'Content-Type: application/json' \
  -H "X-ReviewBot-Token: $GITHUB_WEBHOOK_SECRET" \
  -d '{"repo": "owner/repo", "pr_number": 1}'
```
