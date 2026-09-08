"""Thin wrapper around PyGithub for the operations ReviewBot needs.

Note: this module is ``reviewbot.github``, but ``from github import ...`` below
resolves to the top-level PyGithub package because Python 3 imports are absolute.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from github import Github, GithubException

from reviewbot.utils.config import settings

from .models import GitHubFile, GitHubPR

logger = logging.getLogger(__name__)


class GitHubClient:
    """GitHub API client.

    The underlying PyGithub client is created lazily so that importing this module
    (and therefore starting the API) does not require a token to be configured.
    """

    def __init__(self, token: Optional[str] = None):
        self._token = token or settings.github_token
        self._gh: Optional[Github] = None

    @property
    def gh(self) -> Github:
        if self._gh is None:
            if not self._token:
                raise ValueError(
                    "GitHub token not provided. Set GITHUB_TOKEN in your environment "
                    "or pass token= to GitHubClient()."
                )
            self._gh = Github(self._token)
        return self._gh

    # -- Reads -------------------------------------------------------------

    def get_repo(self, repo_name: str):
        """Get repository by full name (``owner/repo``)."""
        try:
            return self.gh.get_repo(repo_name)
        except GithubException as exc:
            logger.error("Failed to get repo %s: %s", repo_name, exc)
            raise

    def get_pr(self, repo_name: str, pr_number: int):
        """Get pull request by number."""
        try:
            return self.get_repo(repo_name).get_pull(pr_number)
        except GithubException as exc:
            logger.error("Failed to get PR %s#%s: %s", repo_name, pr_number, exc)
            raise

    def get_pr_files(self, repo_name: str, pr_number: int) -> List[GitHubFile]:
        """Get all changed files in a PR."""
        pr = self.get_pr(repo_name, pr_number)
        return [
            GitHubFile(
                filename=f.filename,
                status=f.status,
                additions=f.additions,
                deletions=f.deletions,
                changes=f.changes,
                patch=f.patch,
                raw_url=f.raw_url,
                blob_url=f.blob_url,
            )
            for f in pr.get_files()
        ]

    def get_pr_diff(self, repo_name: str, pr_number: int) -> str:
        """Get the PR diff as text (per-file patches concatenated)."""
        chunks = []
        for f in self.get_pr_files(repo_name, pr_number):
            if f.patch:
                chunks.append(f"--- {f.filename}\n{f.patch}")
        return "\n\n".join(chunks)

    def get_pr_summary(self, repo_name: str, pr_number: int) -> GitHubPR:
        """Normalized metadata for a PR."""
        pr = self.get_pr(repo_name, pr_number)
        return GitHubPR(
            number=pr.number,
            title=pr.title,
            body=pr.body,
            state=pr.state,
            user=pr.user.login if pr.user else "unknown",
            repo=repo_name,
            head_sha=pr.head.sha,
            base_sha=pr.base.sha,
            created_at=pr.created_at,
            updated_at=pr.updated_at,
            html_url=pr.html_url,
        )

    def get_commit(self, repo_name: str, commit_sha: str):
        """Get commit details."""
        return self.get_repo(repo_name).get_commit(commit_sha)

    # -- Writes ------------------------------------------------------------

    def post_comment(self, repo_name: str, pr_number: int, body: str):
        """Post a conversation-level comment on a PR."""
        try:
            pr = self.get_pr(repo_name, pr_number)
            comment = pr.create_issue_comment(body)
            logger.info("Posted comment on PR #%s", pr_number)
            return comment
        except GithubException as exc:
            logger.error("Failed to post comment: %s", exc)
            raise

    def post_inline_comment(
        self,
        repo_name: str,
        pr_number: int,
        commit_sha: str,
        path: str,
        line: int,
        body: str,
    ):
        """Post an inline review comment on a specific line of the new file.

        ``line`` must be a line that appears on the right-hand side of the PR diff;
        GitHub rejects anything else with a 422.
        """
        pr = self.get_pr(repo_name, pr_number)
        commit = self.get_commit(repo_name, commit_sha)
        return self._create_review_comment(pr, commit, path, line, body)

    def post_inline_comments(
        self,
        repo_name: str,
        pr_number: int,
        commit_sha: str,
        comments: Iterable[Dict[str, Any]],
    ) -> int:
        """Post many inline comments, reusing one PR and commit lookup.

        Each comment dict needs ``file``, ``line`` and ``body``. Individual failures
        are logged and skipped (a stale line number should not abort the review).
        Returns the number of comments GitHub accepted.
        """
        comments = list(comments)
        if not comments:
            return 0

        pr = self.get_pr(repo_name, pr_number)
        commit = self.get_commit(repo_name, commit_sha)

        posted = 0
        for comment in comments:
            try:
                self._create_review_comment(
                    pr, commit, comment["file"], comment["line"], comment["body"]
                )
                posted += 1
                comment["posted"] = True
            except Exception as exc:  # noqa: BLE001 - keep going on per-line failures
                comment["posted"] = False
                logger.error(
                    "Failed to post inline comment on %s:%s: %s",
                    comment.get("file"),
                    comment.get("line"),
                    exc,
                )
        return posted

    def _create_review_comment(self, pr, commit, path: str, line: int, body: str):
        result = pr.create_review_comment(
            body=body,
            commit=commit,
            path=path,
            line=line,
            side="RIGHT",
        )
        logger.info("Posted inline comment on %s:%s", path, line)
        return result
