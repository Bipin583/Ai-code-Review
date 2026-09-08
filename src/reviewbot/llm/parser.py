"""Diff annotation and GitHub comment rendering."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

ISSUE_TYPES = ("bugs", "security", "smells", "performance", "best_practices")

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

EMOJI_MAP = {
    "bugs": "\U0001f41b",  # bug
    "security": "\U0001f512",  # lock
    "smells": "\U0001f443",  # nose
    "performance": "⚡",  # zap
    "best_practices": "✅",  # check
}

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def annotate_diff(patch: str) -> Tuple[str, Set[int]]:
    """Prefix every diff line with its line number in the *new* file.

    The model cannot reliably infer file line numbers from a bare unified diff, so
    we hand them to it explicitly. The returned set is the lines GitHub will accept
    an inline comment on (added + context lines on the RIGHT side).

    Returns ``(annotated_text, commentable_line_numbers)``.
    """
    if not patch:
        return "", set()

    out: List[str] = []
    valid: Set[int] = set()
    new_line = 0

    for raw in patch.splitlines():
        hunk = _HUNK_RE.match(raw)
        if hunk:
            new_line = int(hunk.group(3))
            out.append(f"{'':>6}   | {raw}")
            continue

        # File headers before the first hunk, and "\ No newline at end of file".
        if new_line == 0 or raw.startswith("\\"):
            out.append(f"{'':>6}   | {raw}")
            continue

        if raw.startswith("-"):
            out.append(f"{'':>6} - | {raw[1:]}")
            continue

        marker = "+" if raw.startswith("+") else " "
        content = raw[1:] if raw[:1] in ("+", " ") else raw
        out.append(f"{new_line:>6} {marker} | {content}")
        valid.add(new_line)
        new_line += 1

    return "\n".join(out), valid


def strip_code_fences(text: str) -> str:
    """Remove ``` fences and any prose around a JSON object."""
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def get_severity_badge(severity: str) -> str:
    """Human-readable severity badge."""
    badges = {
        "high": "\U0001f534 High",
        "medium": "\U0001f7e1 Medium",
        "low": "\U0001f7e2 Low",
    }
    return badges.get((severity or "").lower(), "⚪ Medium")


def format_inline_comments(
    review: Dict[str, Any],
    filename: Optional[str] = None,
    valid_lines: Optional[Iterable[int]] = None,
) -> List[Dict[str, Any]]:
    """Render one file review into GitHub inline-comment payloads.

    Issues whose line is missing, or not present on the right-hand side of the diff,
    are dropped: GitHub answers those with a 422. They still appear in the PR
    summary comment, so nothing is silently lost.
    """
    filename = filename or review.get("filename") or "unknown"
    allowed = set(valid_lines) if valid_lines is not None else review.get("valid_lines")
    allowed = set(allowed) if allowed else None

    comments: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, int, str, str]] = set()
    confidence = float(review.get("confidence") or 0.0)

    for issue_type in ISSUE_TYPES:
        for issue in review.get(issue_type) or []:
            if not isinstance(issue, dict):
                continue

            line = issue.get("line")
            if not isinstance(line, int) or line <= 0:
                continue
            if allowed is not None and line not in allowed:
                logger.debug(
                    "Skipping inline comment on %s:%s (not in diff)", filename, line
                )
                continue

            description = (issue.get("description") or "").strip()
            if not description:
                continue

            key = (filename, line, issue_type, description)
            if key in seen:
                continue
            seen.add(key)

            severity = (issue.get("severity") or "medium").lower()
            suggestion = (issue.get("suggestion") or "").strip()
            label = issue_type.replace("_", " ").title()
            emoji = EMOJI_MAP.get(issue_type, "\U0001f4dd")

            body = f"{emoji} **{label}** {get_severity_badge(severity)}\n\n{description}"
            if suggestion:
                body += f"\n\n\U0001f4a1 **Suggestion:**\n{suggestion}"
            body += f"\n\n---\n*Confidence: {confidence:.0%} — ReviewBot AI*"

            comments.append(
                {
                    "file": filename,
                    "line": line,
                    "body": body,
                    "type": issue_type,
                    "severity": severity,
                    "confidence": confidence,
                }
            )

    comments.sort(key=lambda c: (SEVERITY_ORDER.get(c["severity"], 1), c["line"]))
    return comments


def format_pr_comment(review_result: Dict[str, Any]) -> str:
    """The body of the PR-level summary comment."""
    return review_result.get("summary") or "No summary available"
