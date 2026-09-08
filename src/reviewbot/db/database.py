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
    """Create any missing tables."""
    from reviewbot.db import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
    logger.debug("Schema ensured for %s", DATABASE_URL)
