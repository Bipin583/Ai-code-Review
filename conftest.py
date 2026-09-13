"""Pytest bootstrap: import paths, throwaway database, shared fixtures.

Environment variables are set *before* ``reviewbot`` is imported, because the
settings object and the SQLAlchemy engine are built at import time.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
for path in (SRC, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_TMP_DIR = tempfile.mkdtemp(prefix="reviewbot-tests-")

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP_DIR).as_posix()}/test.db"
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-anthropic-token")

WEBHOOK_SECRET = os.environ["GITHUB_WEBHOOK_SECRET"]

SAMPLE_DIFF = """@@ -1,4 +1,8 @@
 import os
 
-def divide(a, b):
-    return a / b
+def divide(a, b):
+    return a / b
+
+def run(cmd):
+    os.system(cmd)
+    return True
"""


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Drop the throwaway database directory."""
    try:
        from reviewbot.db.database import engine

        engine.dispose()
    except Exception:  # pragma: no cover - best effort
        pass
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """Create the schema once for the whole test session."""
    from reviewbot.db.database import init_db

    init_db()
    yield


@pytest.fixture(autouse=True)
def clean_tables():
    """Every test starts with empty tables and no cached repo configs."""
    from reviewbot.db.database import SessionLocal
    from reviewbot.db.models import Review, ReviewComment
    from reviewbot.utils.repo_config import clear_config_cache

    clear_config_cache()

    def _truncate():
        db = SessionLocal()
        try:
            db.query(ReviewComment).delete()
            db.query(Review).delete()
            db.commit()
        finally:
            db.close()

    _truncate()
    yield
    _truncate()


@pytest.fixture
def db_session():
    """A database session for tests that need to seed rows."""
    from reviewbot.db.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client():
    """FastAPI test client with the lifespan (and therefore init_db) executed."""
    from fastapi.testclient import TestClient

    from reviewbot.api.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_diff() -> str:
    """A small unified diff containing a divide-by-zero risk and a shell injection."""
    return SAMPLE_DIFF


@pytest.fixture
def sample_review() -> dict:
    """A model response already parsed into the internal review shape."""
    return {
        "bugs": [
            {
                "line": 4,
                "description": "Division by zero when b == 0",
                "severity": "high",
                "suggestion": "Raise ValueError when b == 0",
            }
        ],
        "security": [
            {
                "line": 7,
                "description": "os.system with unsanitised input allows shell injection",
                "severity": "high",
                "suggestion": "Use subprocess.run with a list of arguments",
            }
        ],
        "smells": [],
        "performance": [],
        "best_practices": [
            {
                "line": 3,
                "description": "Missing type hints and docstring",
                "severity": "low",
                "suggestion": "Add type hints",
            }
        ],
        "summary": "Two high-severity issues found.",
        "confidence": 0.9,
        "valid_lines": {1, 2, 3, 4, 5, 6, 7, 8},
        "filename": "app.py",
    }
