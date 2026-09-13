"""GitHub webhook endpoint and the background review pipeline."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from reviewbot.db.database import SessionLocal
from reviewbot.db.models import Review, ReviewComment
from reviewbot.github.client import GitHubClient
from reviewbot.github.models import GitHubFile
from reviewbot.llm.parser import (
    SEVERITY_ORDER,
    format_inline_comments,
    format_pr_comment,
)
from reviewbot.llm.reviewer import CodeReviewer
from reviewbot.utils.config import settings
from reviewbot.utils.repo_config import (
    EffectiveReviewConfig,
    get_repo_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhooks"])

REVIEWABLE_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}

# Column on Review that stores each issue type.
_ISSUE_COLUMNS = {
    "bugs": "bugs",
    "security": "security_issues",
    "smells": "code_smells",
    "performance": "performance_issues",
    "best_practices": "best_practices",
}


def verify_signature(payload: bytes, signature: Optional[str]) -> bool:
    """Verify GitHub's ``X-Hub-Signature-256`` HMAC over the raw request body."""
    secret = settings.github_webhook_secret
    if not secret:
        # Never accept unverified payloads in production: anyone could make the bot
        # comment on arbitrary repositories your token can reach.
        if settings.is_production:
            logger.error("GITHUB_WEBHOOK_SECRET is not set; rejecting webhook")
            return False
        logger.warning(
            "GITHUB_WEBHOOK_SECRET is not set - skipping signature verification "
            "(development only)"
        )
        return True

    if not signature or not signature.startswith("sha256="):
        return False

    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.split("=", 1)[1])


@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: Optional[str] = Header(default=None),
    x_hub_signature_256: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Receive GitHub webhooks.

    The signature is computed over the exact bytes GitHub sent, so the raw body is
    read from the request rather than declared as a parameter.
    """
    body = await request.body()

    if not verify_signature(body, x_hub_signature_256):
        logger.warning("Rejected webhook with an invalid signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Body is not valid JSON")

    event = (x_github_event or "").lower()

    if event == "ping":
        return {"status": "pong", "zen": payload.get("zen")}

    if event != "pull_request":
        return {"status": "ignored", "reason": f"event {event or 'unknown'} not handled"}

    action = payload.get("action")
    if action not in REVIEWABLE_ACTIONS:
        return {"status": "ignored", "reason": f"action {action} not handled"}

    pull_request = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}
    repo_name = repository.get("full_name")
    pr_number = pull_request.get("number") or payload.get("number")

    if not repo_name or not pr_number:
        raise HTTPException(status_code=400, detail="Missing repository or PR number")

    if pull_request.get("draft") and action != "ready_for_review":
        return {"status": "ignored", "reason": "draft pull request"}

    commit_sha = (pull_request.get("head") or {}).get("sha")

    background_tasks.add_task(
        process_pr_review,
        repo_name,
        pr_number,
        commit_sha,
        incremental=(action == "synchronize"),
    )
    logger.info("Queued review for %s#%s (%s)", repo_name, pr_number, action)

    return {
        "status": "accepted",
        "repo": repo_name,
        "pr_number": pr_number,
        "action": action,
    }


def _default_config() -> "EffectiveReviewConfig":
    """Global-settings config, built fresh so monkeypatched settings win in tests."""
    return EffectiveReviewConfig.from_repo_config(None)


def reviewable_files(
    files: Sequence[GitHubFile], config: Optional[Any] = None
) -> List[GitHubFile]:
    """Keep files that were changed, still exist, and pass the effective path filter."""
    cfg = config if config is not None else _default_config()
    keep: List[GitHubFile] = []
    for file in files:
        if file.status == "removed" or not file.patch:
            continue
        if not cfg.is_reviewable_path(file.filename):
            continue
        keep.append(file)
    return keep


def collect_inline_comments(
    result: Dict[str, Any], config: Optional[Any] = None
) -> List[Dict[str, Any]]:
    """Flatten per-file inline comments, severity-gated, most severe first, capped."""
    cfg = config if config is not None else _default_config()
    threshold = SEVERITY_ORDER[cfg.min_severity]
    comments: List[Dict[str, Any]] = []
    for review in result.get("file_reviews", []):
        comments.extend(
            format_inline_comments(
                review,
                filename=review.get("filename"),
                valid_lines=review.get("valid_lines"),
            )
        )

    gated = [
        c for c in comments if SEVERITY_ORDER.get(c["severity"], 1) <= threshold
    ]
    if len(gated) < len(comments):
        logger.info(
            "Severity threshold %s dropped %s inline comment(s)",
            cfg.min_severity,
            len(comments) - len(gated),
        )

    gated.sort(key=lambda c: (SEVERITY_ORDER.get(c["severity"], 1), c["file"], c["line"]))

    limit = cfg.max_inline_comments
    if len(gated) > limit:
        logger.info("Capping inline comments at %s (had %s)", limit, len(gated))
        gated = gated[:limit]
    return gated


def get_last_reviewed_sha(repo_name: str, pr_number: int) -> Optional[str]:
    """Head SHA of the most recent stored review for this PR, if any."""
    db = SessionLocal()
    try:
        row = (
            db.query(Review.commit_sha)
            .filter(Review.repo_name == repo_name, Review.pr_number == pr_number)
            .order_by(Review.created_at.desc(), Review.id.desc())
            .first()
        )
        return row[0] if row else None
    finally:
        db.close()


def persist_review(
    repo_name: str,
    pr_number: int,
    commit_sha: Optional[str],
    result: Dict[str, Any],
    inline_comments: Sequence[Dict[str, Any]],
    *,
    base_commit_sha: Optional[str] = None,
) -> Tuple[Optional[int], List[int]]:
    """Store the review and its rendered comments. Returns ``(review_id, comment_ids)``."""
    db = SessionLocal()
    try:
        review = Review(
            pr_number=pr_number,
            repo_name=repo_name,
            commit_sha=commit_sha,
            base_commit_sha=base_commit_sha,
            summary=result.get("summary"),
            walkthrough=result.get("walkthrough"),
            confidence_score=result.get("average_confidence", 0.0),
            files_reviewed=len(result.get("file_reviews", [])),
            **{
                column: result.get(issue_type, [])
                for issue_type, column in _ISSUE_COLUMNS.items()
            },
        )
        db.add(review)
        db.flush()

        rows = [
            ReviewComment(
                review_id=review.id,
                file_path=comment["file"],
                line_number=comment["line"],
                comment=comment["body"],
                comment_type=comment["type"],
                severity=comment["severity"],
                confidence=comment["confidence"],
            )
            for comment in inline_comments
        ]
        db.add_all(rows)
        db.commit()
        return review.id, [row.id for row in rows]
    except Exception:
        db.rollback()
        logger.exception("Failed to persist review for %s#%s", repo_name, pr_number)
        return None, []
    finally:
        db.close()


def mark_posted(comment_ids: Sequence[int]) -> None:
    """Flag the comments GitHub accepted."""
    if not comment_ids:
        return
    db = SessionLocal()
    try:
        db.query(ReviewComment).filter(ReviewComment.id.in_(list(comment_ids))).update(
            {"posted": 1}, synchronize_session=False
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to flag posted comments")
    finally:
        db.close()


def process_pr_review(
    repo_name: str,
    pr_number: int,
    commit_sha: Optional[str] = None,
    *,
    incremental: bool = False,
) -> Optional[int]:
    """Review a pull request end to end: fetch, review, store, comment.

    Runs in a FastAPI background task, so it swallows its own exceptions - a failed
    review must not take the worker down. Returns the stored review id, if any.

    ``incremental`` (set for ``synchronize`` webhooks) reviews only the changes
    since the last stored review of this PR, falling back to a full review
    whenever the delta cannot be computed safely.
    """
    logger.info("Starting review of %s#%s", repo_name, pr_number)
    try:
        client = GitHubClient()

        # Head, base and title are all needed up front: the base sha selects the
        # repo config, the title feeds the walkthrough.
        pr = client.get_pr(repo_name, pr_number)
        if not commit_sha:
            commit_sha = pr.head.sha
        base_sha = pr.base.sha
        pr_title = pr.title

        # Per-repo config is read at the PR base sha, so a pull request cannot
        # weaken its own review.
        repo_config = get_repo_config(client, repo_name, ref=base_sha)
        cfg = EffectiveReviewConfig.from_repo_config(repo_config)
        if not cfg.enabled:
            logger.info("ReviewBot disabled for %s via .reviewbot.yaml", repo_name)
            return None

        files: Optional[List[GitHubFile]] = None
        range_base: Optional[str] = None
        if incremental:
            last_sha = get_last_reviewed_sha(repo_name, pr_number)
            if last_sha and last_sha == commit_sha:
                logger.info(
                    "Head %s already reviewed for %s#%s; nothing to do",
                    (commit_sha or "")[:7],
                    repo_name,
                    pr_number,
                )
                return None
            if last_sha:
                try:
                    comparison = client.compare_commits(
                        repo_name, last_sha, commit_sha
                    )
                except Exception as exc:  # noqa: BLE001 - fall back to a full review
                    logger.warning(
                        "Compare %s..%s failed (%s); doing a full review",
                        last_sha[:7],
                        (commit_sha or "")[:7],
                        exc,
                    )
                else:
                    if comparison.behind_by > 0:
                        # Force-push or rebase: the diff would not be "what is
                        # new" and would mislead the model.
                        logger.warning(
                            "History diverged for %s#%s; doing a full review",
                            repo_name,
                            pr_number,
                        )
                    else:
                        files = comparison.files
                        range_base = last_sha

        if files is None:
            files = client.get_pr_files(repo_name, pr_number)

        files = reviewable_files(files, cfg)
        if not files:
            logger.info(
                "No reviewable files in %s#%s (extensions: %s)",
                repo_name,
                pr_number,
                ", ".join(cfg.extensions),
            )
            return None

        reviewer = CodeReviewer(config=cfg)
        result = reviewer.review_multiple_files(
            [{"filename": f.filename, "diff": f.patch or ""} for f in files],
            context={
                "pr_title": pr_title,
                "incremental": range_base is not None,
                "base_sha": range_base,
                "head_sha": commit_sha,
            },
        )

        inline_comments = collect_inline_comments(result, cfg)
        review_id, comment_ids = persist_review(
            repo_name,
            pr_number,
            commit_sha,
            result,
            inline_comments,
            base_commit_sha=range_base,
        )

        client.post_comment(repo_name, pr_number, format_pr_comment(result))

        if inline_comments:
            posted = client.post_inline_comments(
                repo_name, pr_number, commit_sha, inline_comments
            )
            logger.info("Posted %s/%s inline comment(s)", posted, len(inline_comments))
            mark_posted(
                [
                    comment_id
                    for comment_id, comment in zip(comment_ids, inline_comments)
                    if comment.get("posted")
                ]
            )

        logger.info(
            "Review complete for %s#%s: %s issue(s) across %s file(s)",
            repo_name,
            pr_number,
            result.get("total_issues", 0),
            len(result.get("file_reviews", [])),
        )
        return review_id
    except Exception:
        logger.exception("Review failed for %s#%s", repo_name, pr_number)
        return None
