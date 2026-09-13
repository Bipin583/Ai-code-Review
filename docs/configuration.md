# Configuration

Every setting is an environment variable, read from the process environment first
and a `.env` file at the repository root second. `configs/settings.py` re-exports
the same object, so `reviewbot.utils.config.settings` and `configs.settings`
resolve to one instance.

Settings are cached (`@lru_cache` on `get_settings`), so changing `.env` requires a
restart.

## Reference

### GitHub

| Variable | Default | Effect |
| --- | --- | --- |
| `GITHUB_TOKEN` | `""` | Token used for all GitHub calls. Empty → `GitHubClient` raises on first use; the app still imports and `/health` still answers. |
| `GITHUB_WEBHOOK_SECRET` | `""` | HMAC secret for `X-Hub-Signature-256`, and the value `POST /api/review` requires as `X-ReviewBot-Token`. Empty in development skips verification with a warning; empty in production rejects every webhook. |

### Model provider

| Variable | Default | Effect |
| --- | --- | --- |
| `ANTHROPIC_AUTH_TOKEN` | `""` | Bearer token for the Anthropic Messages endpoint. Empty → reviews fail with a clear `ValueError`. |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Anthropic Messages-compatible API base URL. |
| `ANTHROPIC_MODEL` | `gpt-5.6-sol` | Model identifier, exactly as the provider names it. |
| `LLM_TEMPERATURE` | `0.2` | Low keeps findings deterministic and reduces invented issues. |
| `LLM_MAX_TOKENS` | `2000` | Ceiling on one file's review. Too low truncates the JSON and the response fails to parse. |
| `LLM_MAX_RETRIES` | `2` | Retries *after* the first attempt, with `2**attempt` second backoff — so 3 attempts and ~3s of waiting by default. |

### Review guardrails

These controls limit per-file input and visible comments. File count is unlimited by
default; set a positive limit only when you need a hard cost ceiling.

| Variable | Default | Effect |
| --- | --- | --- |
| `MAX_FILES_PER_REVIEW` | `0` | `0` reviews every eligible file; a positive value caps calls and lists excess files in `skipped_files` and the summary. |
| `MAX_DIFF_CHARS` | `12000` | Per-file cap on the annotated diff; longer diffs are truncated with a marker and a warning is logged. |
| `MAX_INLINE_COMMENTS` | `25` | Cap across the whole PR, applied after a global severity sort — high-severity findings survive the cut. |
| `REVIEW_FILE_EXTENSIONS` | `.py` | Comma-separated suffixes. Files not matching are skipped before any model call. |

### Database and app

| Variable | Default | Effect |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./data/reviewbot.db` | SQLAlchemy URL. Relative SQLite paths are resolved against the repository root, not the working directory. |
| `APP_ENV` | `development` | `production` (or `prod`) hardens behaviour: unsigned webhooks are rejected, and the manual trigger is disabled without a secret. |
| `LOG_LEVEL` | `INFO` | Standard logging level name. |
| `CORS_ORIGINS` | `*` | Comma-separated origins. Credentials are only allowed when the list is not a wildcard, because browsers reject that combination. |

## Derived properties

`Settings` exposes a few computed helpers used across the app:

| Property | Returns |
| --- | --- |
| `is_production` | `True` when `APP_ENV` is `production` or `prod` |
| `is_sqlite` | `True` when `DATABASE_URL` starts with `sqlite` — gates the `check_same_thread` connect arg |
| `cors_origin_list` | `CORS_ORIGINS` split on commas |
| `review_extensions` | `REVIEW_FILE_EXTENSIONS` as a tuple, each entry dot-prefixed, ready for `str.endswith` |

## Why plain strings, not lists

`CORS_ORIGINS` and `REVIEW_FILE_EXTENSIONS` are typed `str` and parsed by a
property rather than declared as `List[str]`. pydantic-settings tries to JSON-decode
complex types read from the environment, so a `List[str]` field makes
`CORS_ORIGINS=*` a startup crash and demands `["*"]` instead. Comma-separated
strings keep `.env` files readable.

## Per-repository configuration (`.reviewbot.yaml`)

Environment variables configure the bot globally. A repository can override part of
that behaviour by committing a `.reviewbot.yaml` at its root:

```yaml
# Only review files under src/ (quote globs: a bare * starts a YAML alias)
include:
  - "src/**"
# ...except generated code and fixtures
exclude:
  - "src/legacy/**"
  - "*_generated.py"
# Review more than just Python
extensions: [py, js, ts]
# Fewer inline comments, higher bar
max_inline_comments: 10
min_severity: medium
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `include` | list of globs | — | When set, a file must match at least one glob to be reviewed. |
| `exclude` | list of globs | `[]` | Files matching any glob are skipped. |
| `extensions` | list of suffixes | global `REVIEW_FILE_EXTENSIONS` | Replaces the global list entirely (dots optional: `py` ≡ `.py`). |
| `max_inline_comments` | int ≥ 0 | global `MAX_INLINE_COMMENTS` | Per-PR inline comment cap. |
| `min_severity` | `high`/`medium`/`low` | `low` | Inline comments below this severity are not posted; the summary still lists every finding. |
| `enabled` | bool | `true` | `false` disables the bot for the repository — no comments, no database row. |

Rules worth knowing:

- **Globs are `fnmatch` patterns** matched case-sensitively against the repo-relative
  POSIX path. `*` crosses `/` (so `*.py` matches `a/b/c.py`); there is no brace
  expansion. Quote patterns starting with `*` in YAML, or the parser reads them as
  aliases.
- **The config is read at the PR *base* sha**, so a pull request cannot weaken its
  own review — adding `.reviewbot.yaml` with `enabled: false` to a PR has no effect
  until that change lands on the base branch through the normal review path.
- **Unknown keys are warned about and ignored**; malformed YAML or invalid values
  fall back to the global config rather than skipping the review.
- `MAX_FILES_PER_REVIEW` and `MAX_DIFF_CHARS` are deliberately **not** overridable:
  they are cost guardrails for the operator running the bot, not repo preferences.
- Configs are cached in-process per `(repo, ref)`, so repeated pushes do not
  refetch the file.

## Using a different provider

The reviewer speaks the Anthropic Messages protocol. Configure any compatible gateway
with these values:

```ini
ANTHROPIC_AUTH_TOKEN=<provider token>
ANTHROPIC_BASE_URL=https://<provider host>
ANTHROPIC_MODEL=<exact model id>
```

For AgentRouter:

```ini
ANTHROPIC_AUTH_TOKEN=<agentrouter token>
ANTHROPIC_BASE_URL=https://agentrouter.org
ANTHROPIC_MODEL=gpt-5.6-sol
```

Confirm the model identifier with the provider; catalogues differ between gateways.
Check what the provider logs, since every diff reviewed is source code leaving your
infrastructure. The JSON-only instruction is part of the system prompt, and the parser
also removes optional markdown fences before decoding the response.
