# Database

Two tables. SQLite by default, PostgreSQL by changing one environment variable.

## Connection handling

`src/reviewbot/db/database.py` owns the engine. Three things it does that matter:

- **Relative SQLite paths are anchored to the project root.** The default
  `sqlite:///./data/reviewbot.db` resolves to `<project>/data/reviewbot.db` regardless
  of the working directory you started from, so the API and the dashboard cannot end up
  reading two different files.
- **`check_same_thread=False` is set only for SQLite.** Reviews run in FastAPI
  background threads, so the connection has to be usable off the request thread.
  Passing that argument to PostgreSQL would be an error, so it is gated on
  `settings.is_sqlite`.
- **`get_db` is a FastAPI dependency** that always closes its session, including when
  the handler raises.

`init_db` creates the tables at startup via the lifespan hook. There is no migration
tool; see the schema-change note at the end.

## `reviews`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | Integer PK | |
| `pr_number` | Integer, indexed | |
| `repo_name` | String, indexed | `owner/repo` |
| `commit_sha` | String | head SHA the review ran against |
| `base_commit_sha` | String, nullable | incremental reviews: the range covered is `base_commit_sha..commit_sha`; NULL on a full review |
| `bugs` | JSON | list of issue objects |
| `security_issues` | JSON | |
| `code_smells` | JSON | |
| `performance_issues` | JSON | |
| `best_practices` | JSON | |
| `summary` | Text | the rendered markdown posted to the PR |
| `walkthrough` | Text, nullable | plain-English "what this PR does" paragraph |
| `confidence_score` | Float | mean across files, 0.0–1.0 |
| `files_reviewed` | Integer | |
| `created_at` | DateTime | naive UTC |
| `updated_at` | DateTime | naive UTC, set on update |

The API's short names differ from the column names — `security` is
`security_issues`, `smells` is `code_smells`, `performance` is
`performance_issues`. `ISSUE_FIELDS` in `api/routes.py` is the mapping; use it rather
than guessing when you write new queries.

Rows are append-only in normal operation: a new push creates a new review rather than
overwriting the old one, so you keep the history of what the bot said on each commit.

## `review_comments`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | Integer PK | |
| `review_id` | Integer FK → `reviews.id`, indexed | `ON DELETE CASCADE` |
| `file_path` | String | |
| `line_number` | Integer | line in the new file |
| `comment` | Text | rendered markdown body |
| `comment_type` | String | `bugs`, `security`, `smells`, `performance`, `best_practices` |
| `severity` | String | `high`, `medium`, `low` |
| `confidence` | Float | |
| `posted` | Integer | `1` if GitHub accepted it, `0` otherwise |
| `created_at` | DateTime | naive UTC |

`Review.comments` uses `cascade="all, delete-orphan"`, so deleting a review through
the ORM removes its comments too.

`posted` is an integer rather than a boolean for portability across both backends,
and the API converts it with `bool()` on the way out.

## Issue payload shape

Every entry in the five JSON columns has the same four keys, plus `file` once the
issue has been aggregated to PR level:

```json
{
  "line": 88,
  "description": "fetchone() returns None when no row matches, so [0] raises TypeError.",
  "severity": "high",
  "suggestion": "row = cur.fetchone()\nif row is None:\n    return default",
  "file": "src/store.py"
}
```

`line` may be `null` — the model omitted it or gave something unusable. Such issues
still appear in the summary and the dashboard; they simply cannot become inline
comments. `severity` is always one of the three values, and `description` is never
empty, because `_normalize` enforces both before anything is stored.

## Timestamps

`utcnow()` in `db/models.py` returns a **naive** UTC datetime. Every timestamp in the
database and in API responses is UTC without a timezone marker
(`2026-09-03T16:04:11.512000`). If you compare these against anything, make it naive
UTC too — mixing naive and aware datetimes raises `TypeError` in Python.

## Useful queries

```sql
-- Highest-signal reviews first
SELECT id, repo_name, pr_number, confidence_score,
       json_array_length(bugs) + json_array_length(security_issues) AS serious
FROM reviews
ORDER BY serious DESC, created_at DESC
LIMIT 20;

-- Inline comments GitHub rejected
SELECT r.repo_name, r.pr_number, c.file_path, c.line_number, c.severity
FROM review_comments c
JOIN reviews r ON r.id = c.review_id
WHERE c.posted = 0
ORDER BY c.id DESC;

-- Review volume per day
SELECT date(created_at) AS day, COUNT(*) AS reviews
FROM reviews GROUP BY day ORDER BY day DESC;
```

`json_array_length` is SQLite; on PostgreSQL use `jsonb_array_length` and note that
the columns are `json`, not `jsonb`, unless you alter them.

Quick look at the local database:

```bash
sqlite3 data/reviewbot.db ".tables"
sqlite3 data/reviewbot.db "SELECT id, repo_name, pr_number, files_reviewed FROM reviews ORDER BY id DESC LIMIT 5;"
```

## Moving to PostgreSQL

```env
DATABASE_URL=postgresql+psycopg2://user:password@host:5432/reviewbot
```

Nothing else changes: `check_same_thread` is dropped automatically, `init_db` creates
the tables, and the JSON columns map to PostgreSQL `json`. Install the driver
(`psycopg2-binary`) and make sure the database exists first. `GET /health` reports
`database_backend` so you can confirm which one you are actually on.

Existing SQLite data is not migrated for you. For a handful of rows, read them
through `GET /api/reviews` and re-insert; for anything larger, dump and transform,
remembering that SQLite stores the JSON columns as text.

## Changing the schema

`init_db` issues `CREATE TABLE IF NOT EXISTS`, which never alters an existing table.
For columns added after the first release it also runs a small built-in migration:
`_add_missing_columns` in `db/database.py` compares the columns named in
`_EXPECTED_COLUMNS` against what the database actually has and issues
`ALTER TABLE ... ADD COLUMN` for the missing ones. Both post-release columns so far
(`reviews.base_commit_sha`, `reviews.walkthrough`) are nullable, which is what makes
plain `ALTER TABLE` portable across SQLite and PostgreSQL.

If you add a column yourself, register it in `_EXPECTED_COLUMNS` (or drop the local
`data/reviewbot.db` and let it be recreated, or adopt Alembic — `pip install alembic`,
`alembic init`, point `sqlalchemy.url` at `DATABASE_URL`, autogenerate against
`Base.metadata`). Alembic is the right answer as soon as you want anything richer
than "add a nullable column", which in practice means before the first production
deploy.
