"""Read APIs for the dashboard, plus a manual review trigger."""

from __future__ import annotations

import hmac
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from reviewbot.db.database import get_db
from reviewbot.db.models import Review, ReviewComment, utcnow
from reviewbot.utils.config import settings

from .webhooks import process_pr_review

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["reviews"])

# Response key -> Review column holding that issue list.
ISSUE_FIELDS = {
    "bugs": "bugs",
    "security": "security_issues",
    "smells": "code_smells",
    "performance": "performance_issues",
    "best_practices": "best_practices",
}


class ManualReviewRequest(BaseModel):
    """Body for POST /api/review."""

    repo: str = Field(..., description="Repository full name, e.g. owner/repo")
    pr_number: int = Field(..., ge=1)
    commit_sha: Optional[str] = None


def issue_lists(review: Review) -> Dict[str, List[Dict[str, Any]]]:
    """The five issue lists of a review, keyed by short name."""
    return {
        key: list(getattr(review, column) or [])
        for key, column in ISSUE_FIELDS.items()
    }


def count_issues(review: Review) -> int:
    return sum(len(issues) for issues in issue_lists(review).values())


def serialize_review(review: Review, include_issues: bool = False) -> Dict[str, Any]:
    """Review -> JSON-safe dict."""
    payload: Dict[str, Any] = {
        "id": review.id,
        "repo_name": review.repo_name,
        "pr_number": review.pr_number,
        "commit_sha": review.commit_sha,
        "summary": review.summary,
        "confidence_score": review.confidence_score or 0.0,
        "files_reviewed": review.files_reviewed or 0,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "updated_at": review.updated_at.isoformat() if review.updated_at else None,
    }
    issues = issue_lists(review)
    payload["issue_counts"] = {key: len(value) for key, value in issues.items()}
    payload["total_issues"] = sum(payload["issue_counts"].values())
    if include_issues:
        payload["issues"] = issues
    return payload


def serialize_comment(comment: ReviewComment) -> Dict[str, Any]:
    """ReviewComment -> JSON-safe dict."""
    return {
        "id": comment.id,
        "review_id": comment.review_id,
        "file_path": comment.file_path,
        "line_number": comment.line_number,
        "comment": comment.comment,
        "comment_type": comment.comment_type,
        "severity": comment.severity,
        "confidence": comment.confidence or 0.0,
        "posted": bool(comment.posted),
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


@router.get("/reviews")
def list_reviews(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    repo: Optional[str] = Query(default=None, description="Filter by owner/repo"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Most recent reviews first."""
    query = db.query(Review)
    if repo:
        query = query.filter(Review.repo_name == repo)

    total = query.count()
    reviews = (
        query.order_by(Review.created_at.desc(), Review.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "reviews": [serialize_review(r) for r in reviews],
    }


@router.get("/reviews/{review_id}")
def get_review(review_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """One review, with its issues and rendered comments."""
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    payload = serialize_review(review, include_issues=True)
    payload["comments"] = [
        serialize_comment(c)
        for c in db.query(ReviewComment)
        .filter(ReviewComment.review_id == review_id)
        .order_by(ReviewComment.file_path, ReviewComment.line_number)
        .all()
    ]
    return payload


@router.get("/reviews/{review_id}/comments")
def get_review_comments(
    review_id: int, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Inline comments belonging to a review."""
    if not db.query(Review.id).filter(Review.id == review_id).first():
        raise HTTPException(status_code=404, detail="Review not found")

    comments = (
        db.query(ReviewComment)
        .filter(ReviewComment.review_id == review_id)
        .order_by(ReviewComment.file_path, ReviewComment.line_number)
        .all()
    )
    return {
        "review_id": review_id,
        "count": len(comments),
        "comments": [serialize_comment(c) for c in comments],
    }


@router.get("/repos")
def list_repos(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Repositories seen so far, with review counts."""
    rows = (
        db.query(
            Review.repo_name,
            func.count(Review.id).label("reviews"),
            func.max(Review.created_at).label("last_review"),
        )
        .group_by(Review.repo_name)
        .order_by(func.count(Review.id).desc())
        .all()
    )
    return {
        "repos": [
            {
                "repo_name": row.repo_name,
                "reviews": row.reviews,
                "last_review": row.last_review.isoformat() if row.last_review else None,
            }
            for row in rows
        ]
    }


@router.get("/metrics")
def get_metrics(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Aggregate metrics for the dashboard."""
    reviews = db.query(Review).all()
    total_reviews = len(reviews)

    totals = {key: 0 for key in ISSUE_FIELDS}
    severity = {"high": 0, "medium": 0, "low": 0}

    for review in reviews:
        for key, issues in issue_lists(review).items():
            totals[key] += len(issues)
            for issue in issues:
                level = str((issue or {}).get("severity", "medium")).lower()
                severity[level if level in severity else "medium"] += 1

    confidences = [r.confidence_score or 0.0 for r in reviews]
    week_ago = utcnow() - timedelta(days=7)

    return {
        "total_reviews": total_reviews,
        "total_issues": sum(totals.values()),
        "total_bugs": totals["bugs"],
        "total_security": totals["security"],
        "total_smells": totals["smells"],
        "total_performance": totals["performance"],
        "total_best_practices": totals["best_practices"],
        "severity_breakdown": severity,
        "average_confidence": (
            sum(confidences) / len(confidences) if confidences else 0.0
        ),
        "total_files_reviewed": sum(r.files_reviewed or 0 for r in reviews),
        "total_comments": db.query(func.count(ReviewComment.id)).scalar() or 0,
        "posted_comments": (
            db.query(func.count(ReviewComment.id))
            .filter(ReviewComment.posted == 1)
            .scalar()
            or 0
        ),
        "repos_reviewed": len({r.repo_name for r in reviews}),
        "reviews_last_7_days": sum(
            1 for r in reviews if r.created_at and r.created_at >= week_ago
        ),
        "issues_per_review": (
            sum(totals.values()) / total_reviews if total_reviews else 0.0
        ),
    }


def _authorize_manual_review(token: Optional[str]) -> None:
    """Gate the manual trigger.

    This endpoint makes the bot write to GitHub, so it is not left open: when
    GITHUB_WEBHOOK_SECRET is configured the caller must present it as
    ``X-ReviewBot-Token``, and in production a missing secret disables the endpoint
    outright.
    """
    secret = settings.github_webhook_secret
    if secret:
        if not token or not hmac.compare_digest(token, secret):
            raise HTTPException(status_code=401, detail="Invalid or missing token")
        return

    if settings.is_production:
        raise HTTPException(
            status_code=503,
            detail="Manual review is disabled: set GITHUB_WEBHOOK_SECRET to enable it",
        )
    logger.warning("Manual review triggered without a token (development only)")


@router.post("/review", status_code=202)
def trigger_review(
    payload: ManualReviewRequest,
    background_tasks: BackgroundTasks,
    x_reviewbot_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Manually queue a review for a pull request."""
    _authorize_manual_review(x_reviewbot_token)

    if "/" not in payload.repo:
        raise HTTPException(status_code=400, detail="repo must be owner/repo")

    background_tasks.add_task(
        process_pr_review, payload.repo, payload.pr_number, payload.commit_sha
    )
    logger.info("Manually queued review for %s#%s", payload.repo, payload.pr_number)
    return {
        "status": "accepted",
        "repo": payload.repo,
        "pr_number": payload.pr_number,
    }
