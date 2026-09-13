"""ORM models for stored reviews."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base


def utcnow() -> datetime:
    """Naive UTC timestamp (``datetime.utcnow`` is deprecated in 3.12+)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Review(Base):
    """One AI review of one pull request head commit.

    The five issue columns hold flat lists of issue dicts; each issue carries the
    ``file`` it came from, so nothing is lost by flattening across files.
    """

    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, index=True)
    pr_number = Column(Integer, nullable=False, index=True)
    repo_name = Column(String, nullable=False, index=True)
    commit_sha = Column(String, index=True)
    # Incremental reviews: the range covered is base_commit_sha..commit_sha.
    # NULL on a full review; it also makes "last reviewed SHA" lookups auditable.
    base_commit_sha = Column(String, nullable=True)

    # Review results (stored as JSON)
    bugs = Column(JSON, default=list)
    security_issues = Column(JSON, default=list)
    code_smells = Column(JSON, default=list)
    performance_issues = Column(JSON, default=list)
    best_practices = Column(JSON, default=list)
    summary = Column(Text)
    walkthrough = Column(Text)  # plain-English "what this PR does" (may be NULL)

    # Metrics
    confidence_score = Column(Float, default=0.0)
    files_reviewed = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime, default=utcnow, index=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    comments = relationship(
        "ReviewComment",
        back_populates="review",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Review {self.repo_name}#{self.pr_number} id={self.id}>"


class ReviewComment(Base):
    """A single rendered inline comment belonging to a review."""

    __tablename__ = "review_comments"

    id = Column(Integer, primary_key=True, index=True)
    review_id = Column(Integer, ForeignKey("reviews.id", ondelete="CASCADE"), index=True)
    file_path = Column(String, nullable=False)
    line_number = Column(Integer)
    comment = Column(Text, nullable=False)
    comment_type = Column(String)  # bugs, security, smells, performance, best_practices
    severity = Column(String)  # high, medium, low
    confidence = Column(Float, default=0.0)
    posted = Column(Integer, default=0)  # 1 once GitHub accepted the inline comment
    created_at = Column(DateTime, default=utcnow)

    review = relationship("Review", back_populates="comments")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ReviewComment {self.file_path}:{self.line_number}>"
