# Development

## Layout

```
src/reviewbot/
  api/       main.py      FastAPI app, lifespan, CORS, /, /health
             routes.py    /api/* read endpoints + manual trigger
             webhooks.py  /webhook/github, signature check, review pipeline
  github/    client.py    PyGithub wrapper: fetch files, post comments
             models.py    GitHubFile / PullRequestInfo dataclasses
  llm/       reviewer.py  prompts, model call, normalization, aggregation
             parser.py    diff annotation, comment rendering, line validation
  db/        database.py  engine, session, init_db, get_db
             models.py    Review, ReviewComment
  dashboard/ app.py       Streamlit UI
  utils/     config.py    Settings (single source of truth)
configs/settings.py       re-export, so `from configs.settings import settings` works
dashboard/app.py          launcher: `streamlit run dashboard/app.py`
tests/                    test_api.py, test_reviewer.py, test_github_client.py
conftest.py               test bootstrap at the repo root
```

`src` layout, so the installed package is what gets imported rather than whatever
happens to be in the working directory. `pyproject.toml` searches both `src` and `.`
(`include = ["reviewbot*", "configs*"]`) so the root-level `configs` package survives
an editable install.

The dependency direction is one-way: `api` uses `github`, `llm` and `db`; those three
know nothing about `api` and nothing about each other, except that `llm/reviewer.py`
imports helpers from `llm/parser.py`. Adding an import that points the other way is
the change to avoid.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
pip install -e ".[dev]"
```

Run the API with reload:

```bash
uvicorn reviewbot.api.main:app --reload --port 8000
```

## Tests

```bash
pytest -q                                  # whole suite
pytest tests/test_reviewer.py -q           # one file
pytest -k signature -q                     # by name
pytest --cov=reviewbot --cov-report=term-missing
```

77 tests across three files, all offline — no network, no API keys, no GitHub. One is
skipped unless `REVIEWBOT_LIVE_TESTS=1` opts into a real provider call.

`conftest.py` sets environment variables **before** `reviewbot` is imported, because
both the settings object and the SQLAlchemy engine are built at import time. It points
`DATABASE_URL` at a throwaway SQLite file in a temp directory, creates the schema once
per session, truncates both tables before every test via an autouse fixture, and
deletes the temp directory in `pytest_sessionfinish`. It also exports `SAMPLE_DIFF`,
a small patch with a division-by-zero and an `os.system` call, for tests that need a
realistic diff.

If you add a setting that must differ under test, set it in `conftest.py` with
`os.environ.setdefault` alongside the others — not inside a test, where it would be
too late.

## Testing without hitting the network

`CodeReviewer.__init__` accepts a client, so a fake is a few lines:

```python
class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.messages = self

    def create(self, **kwargs):
        import json, types
        block = types.SimpleNamespace(type="text", text=json.dumps(self.payload))
        return types.SimpleNamespace(content=[block])

reviewer = CodeReviewer(client=FakeClient({"bugs": [], "summary": "clean", "confidence": 0.9}))
result = reviewer.review_file("app.py", SAMPLE_DIFF)
```

The same trick works for GitHub: `GitHubClient` takes an injectable client, so tests
substitute an object exposing the handful of PyGithub methods actually used.

For the HTTP layer, use FastAPI's `TestClient` and sign the body yourself:

```python
import hashlib, hmac, json
body = json.dumps(payload).encode()
sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
client.post("/webhook/github", content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "pull_request",
                     "Content-Type": "application/json"})
```

Pass `content=body`, not `json=payload` — the signature covers the exact bytes, and
re-serializing changes them.

## Extending the bot

**Review another language.** Set `REVIEW_FILE_EXTENSIONS=.py,.js,.ts`. No code change
needed, but the prompts say "Python" and the best-practices category assumes PEP 8,
so update `SYSTEM_PROMPT` and `USER_PROMPT_TEMPLATE` in `llm/reviewer.py` to match.

**Add an issue category.** Four places: `ISSUE_TYPES` in `llm/parser.py`, the schema
in `SYSTEM_PROMPT`, a JSON column plus `ISSUE_FIELDS` entry for the API, and the
breakdown table in `_generate_summary`. Adding a column means a schema change — see
[database.md](database.md).

**Change the prompt.** Everything is in `SYSTEM_PROMPT` and `USER_PROMPT_TEMPLATE`.
Keep three invariants: the JSON-only instruction, the line-number contract (numbers
come from the annotated diff, never a `-` row), and the `<diff>` wrapper with its
untrusted-content disclaimer.

**Swap the provider or model.** Environment only — `ANTHROPIC_BASE_URL`,
`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`. The client uses the Anthropic Messages API.
See the provider recipe in [configuration.md](configuration.md).

**Post a single GitHub review instead of many comments.** Replace the per-comment loop
in `github/client.py` with `pr.create_review(body=..., comments=[...], event="COMMENT")`.
The tradeoff: one atomic post, but one bad line number rejects the entire batch, where
the current design loses only that comment.

## Conventions

Type hints everywhere, `from __future__ import annotations` at the top of every
module, docstrings that say *why* rather than restating the signature, and
`logger = logging.getLogger(__name__)` per module — never `print`. Broad
`except Exception` is used deliberately at the three isolation boundaries described in
[architecture.md](architecture.md), each with a `# noqa: BLE001` and a comment
explaining what it protects; anywhere else, catch something specific.

`.github/workflows/tests.yml` runs `pytest` with coverage on Python 3.10, 3.11 and
3.12, and builds the Docker image in a separate job, on every push to `main`/`master`
and every pull request.
