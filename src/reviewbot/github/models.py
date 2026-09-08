"""Pydantic view models for the GitHub payloads we care about."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel


class GitHubFile(BaseModel):
    """A single changed file in a pull request."""

    filename: str
    status: str  # added, modified, removed, renamed
    additions: int
    deletions: int
    changes: int
    patch: Optional[str] = None
    raw_url: Optional[str] = None
    blob_url: Optional[str] = None


class GitHubPR(BaseModel):
    """Normalized pull request."""

    number: int
    title: str
    body: Optional[str] = None
    state: str
    user: str
    repo: str
    head_sha: str
    base_sha: str
    created_at: datetime
    updated_at: datetime
    html_url: str


class WebhookPayload(BaseModel):
    """Subset of a GitHub webhook payload used by the handler."""

    action: Optional[str] = None
    number: Optional[int] = None
    pull_request: Optional[Dict[str, Any]] = None
    repository: Dict[str, Any]
    sender: Optional[Dict[str, Any]] = None
