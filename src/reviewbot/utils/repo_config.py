"""Per-repository configuration loaded from a ``.reviewbot.yaml`` file.

The file lives in the repository being reviewed and is read at the PR *base*
sha, so a pull request cannot weaken its own review: config changes take
effect once they land on the base branch through the normal review path.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from reviewbot.utils.config import settings

logger = logging.getLogger(__name__)

CONFIG_FILENAME = ".reviewbot.yaml"

VALID_SEVERITIES = ("high", "medium", "low")

# Config files are stable per commit; a small in-process cache keeps a repeated
# push or dashboard render from refetching the same blob.
_MAX_CACHE_ENTRIES = 128


class RepoConfig(BaseModel):
    """Validated subset of ``.reviewbot.yaml``. Unknown keys are warned and ignored."""

    model_config = ConfigDict(extra="allow")

    include: Optional[List[str]] = None
    exclude: List[str] = []
    extensions: Optional[List[str]] = None
    max_inline_comments: Optional[int] = None
    min_severity: str = "low"
    enabled: bool = True

    @field_validator("extensions")
    @classmethod
    def _normalize_extensions(cls, value):
        if value is None:
            return None
        return [e if e.startswith(".") else f".{e}" for e in value]

    @field_validator("min_severity")
    @classmethod
    def _validate_severity(cls, value: str) -> str:
        value = (value or "").lower().strip()
        if value not in VALID_SEVERITIES:
            raise ValueError(f"min_severity must be one of {VALID_SEVERITIES}")
        return value

    @field_validator("max_inline_comments")
    @classmethod
    def _validate_cap(cls, value):
        if value is not None and value < 0:
            raise ValueError("max_inline_comments must be >= 0")
        return value

    def matches_path(self, filename: str) -> bool:
        """include/exclude glob filter; ``include=None`` means everything."""
        if self.include is not None and not any(
            fnmatch.fnmatchcase(filename, pattern) for pattern in self.include
        ):
            return False
        return not any(fnmatch.fnmatchcase(filename, p) for p in self.exclude)


class EffectiveReviewConfig:
    """Global settings overlaid with one repo's ``.reviewbot.yaml``.

    A plain value object carrying no I/O. ``max_files_per_review`` and
    ``max_diff_chars`` stay global-only: they are cost guardrails for the bot
    operator, not per-repo preferences.
    """

    def __init__(
        self,
        extensions: Tuple[str, ...],
        include: Optional[Tuple[str, ...]],
        exclude: Tuple[str, ...],
        max_inline_comments: int,
        max_files_per_review: int,
        max_diff_chars: int,
        min_severity: str,
        enabled: bool,
    ):
        self.extensions = extensions
        self.include = include
        self.exclude = exclude
        self.max_inline_comments = max_inline_comments
        self.max_files_per_review = max_files_per_review
        self.max_diff_chars = max_diff_chars
        self.min_severity = min_severity
        self.enabled = enabled

    @classmethod
    def from_repo_config(
        cls, repo_config: Optional[RepoConfig]
    ) -> "EffectiveReviewConfig":
        """Global defaults, with repo-level overrides applied where set."""
        cfg = repo_config
        return cls(
            extensions=(
                tuple(cfg.extensions)
                if cfg and cfg.extensions
                else settings.review_extensions
            ),
            include=(
                tuple(cfg.include) if cfg and cfg.include is not None else None
            ),
            exclude=tuple(cfg.exclude) if cfg else (),
            max_inline_comments=(
                cfg.max_inline_comments
                if cfg and cfg.max_inline_comments is not None
                else settings.max_inline_comments
            ),
            max_files_per_review=settings.max_files_per_review,
            max_diff_chars=settings.max_diff_chars,
            min_severity=cfg.min_severity if cfg else "low",
            enabled=cfg.enabled if cfg else True,
        )

    def is_reviewable_path(self, filename: str) -> bool:
        """Extensions plus include/exclude path filter."""
        if not filename.endswith(self.extensions):
            return False
        if self.include is not None and not any(
            fnmatch.fnmatchcase(filename, p) for p in self.include
        ):
            return False
        return not any(fnmatch.fnmatchcase(filename, p) for p in self.exclude)


def parse_repo_config(content: str, source: str = CONFIG_FILENAME) -> Optional[RepoConfig]:
    """YAML text -> ``RepoConfig``.

    Malformed YAML, a non-mapping root, or invalid values log a warning and
    return None, so the caller falls back to the global config rather than
    skipping the review.
    """
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        logger.warning("Could not parse %s (%s); using global config", source, exc)
        return None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning("%s must be a mapping; using global config", source)
        return None
    try:
        config = RepoConfig.model_validate(raw)
    except ValueError as exc:  # pydantic ValidationError subclasses ValueError
        logger.warning("Invalid %s (%s); using global config", source, exc)
        return None
    if config.model_extra:
        logger.warning(
            "Ignoring unknown .reviewbot.yaml key(s): %s",
            ", ".join(config.model_extra),
        )
    return config


# Keyed by (repo_name, ref). A config is stable per commit.
_config_cache: Dict[Tuple[str, str], Optional[RepoConfig]] = {}


def get_repo_config(
    client: Any, repo_name: str, ref: Optional[str]
) -> Optional[RepoConfig]:
    """Fetch and parse ``.reviewbot.yaml`` at a ref, cached in-process.

    Returns None when the repository has no config file (the common case).
    """
    key = (repo_name, ref or "")
    if key in _config_cache:
        return _config_cache[key]
    content = client.get_contents(repo_name, CONFIG_FILENAME, ref=ref)
    config = parse_repo_config(content) if content else None
    if len(_config_cache) >= _MAX_CACHE_ENTRIES:
        _config_cache.clear()
    _config_cache[key] = config
    return config


def clear_config_cache() -> None:
    """Test helper: forget every cached repo config."""
    _config_cache.clear()
