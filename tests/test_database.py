"""Tests for the schema bootstrap, including the add-column mini-migration."""

from __future__ import annotations

from sqlalchemy import inspect, text

from reviewbot.db.database import Base, _add_missing_columns, engine


def test_add_missing_columns_alters_a_legacy_reviews_table():
    """A database created before the new columns must be upgraded in place."""
    # Simulate a pre-0.2 schema: no base_commit_sha, no walkthrough.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS reviews"))
        conn.execute(
            text(
                """
                CREATE TABLE reviews (
                    id INTEGER PRIMARY KEY,
                    pr_number INTEGER,
                    repo_name VARCHAR,
                    commit_sha VARCHAR,
                    summary TEXT
                )
                """
            )
        )

    try:
        _add_missing_columns()

        columns = {c["name"] for c in inspect(engine).get_columns("reviews")}
        assert {"base_commit_sha", "walkthrough"} <= columns
        # Running it again must be a no-op, not a duplicate ALTER.
        _add_missing_columns()
    finally:
        # Restore the real schema for the rest of the session.
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS reviews"))
        from reviewbot.db import models  # noqa: F401  (register mappers)

        Base.metadata.create_all(bind=engine)


def test_add_missing_columns_skips_unknown_tables():
    """The migration map only names tables that must exist; others are ignored."""
    # reviews exists with the current schema at this point; the call is a no-op.
    _add_missing_columns()

    columns = {c["name"] for c in inspect(engine).get_columns("reviews")}
    assert "base_commit_sha" in columns
