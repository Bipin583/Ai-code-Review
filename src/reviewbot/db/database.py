"""SQLAlchemy engine, session factory and schema bootstrap."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from reviewbot.utils.config import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)


def _normalize_database_url(url: str) -> str:
    """Anchor relative SQLite paths to the repo root and create the directory.

    ``sqlite:///./data/reviewbot.db`` is resolved against the working directory by
    SQLAlchemy, which breaks as soon as the app is started from somewhere else
    (uvicorn --app-dir, Docker, the dashboard). Resolve it once, here.
    """
    if not url.startswith("sqlite"):
        return url

    parts = urlsplit(url)
    raw_path = parts.path.lstrip("/")
    if not raw_path or raw_path == ":memory:":
        return url

    path = Path(raw_path)
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    return f"sqlite:///{path.as_posix()}"


DATABASE_URL = _normalize_database_url(settings.database_url)

# check_same_thread=False is required because FastAPI background tasks touch the
# session from a worker thread. It is a SQLite-only connect arg.
_connect_args = {"check_same_thread": False} if settings.is_sqlite else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency yielding a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create any missing tables, then add any columns introduced later."""
    from reviewbot.db import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    logger.debug("Schema ensured for %s", DATABASE_URL)


# Columns added after the first release. create_all does not ALTER existing
# tables and this project has no Alembic, so these are added by hand. All are
# nullable, which makes plain ALTER TABLE ... ADD COLUMN portable across
# SQLite and PostgreSQL. Keep in sync with db/models.py.
_EXPECTED_COLUMNS = {
    "reviews": {
        "base_commit_sha": "VARCHAR",
        "walkthrough": "TEXT",
    }
}


def _add_missing_columns() -> None:
    """ALTER TABLE ... ADD COLUMN for schema drift between releases."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    for table, columns in _EXPECTED_COLUMNS.items():
        if table not in inspector.get_table_names():
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, ddl_type in columns.items():
            if name in existing:
                continue
            with engine.begin() as conn:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")
                )
            logger.info("Added column %s.%s", table, name)
