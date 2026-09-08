"""Application settings.

This module is the single source of truth for configuration. ``configs/settings.py``
re-exports it so both ``reviewbot.utils.config`` and ``configs.settings`` resolve to
the same ``Settings`` instance.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root: src/reviewbot/utils/config.py -> up 4 levels
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Environment-backed configuration.

    Field names map to upper-case environment variables (``github_token`` ->
    ``GITHUB_TOKEN``) and are also read from a ``.env`` file at the repo root.
    """

    # GitHub
    github_token: str = ""
    github_webhook_secret: str = ""

    # Anthropic Messages API (including compatible gateways such as AgentRouter)
    anthropic_auth_token: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "gpt-5.6-sol"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2000
    llm_max_retries: int = 2

    # Review guardrails: keep a single PR from costing an unbounded amount.
    # Zero reviews every eligible file; a positive value restores a per-PR cap.
    max_files_per_review: int = 0
    max_diff_chars: int = 12_000
    max_inline_comments: int = 25
    review_file_extensions: str = ".py"

    # Database
    database_url: str = "sqlite:///./data/reviewbot.db"

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    cors_origins: str = "*"

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Derived helpers ---------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def cors_origin_list(self) -> List[str]:
        """CORS origins as a list. ``*`` stays a single wildcard entry."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def review_extensions(self) -> tuple[str, ...]:
        """File suffixes eligible for review, e.g. ``('.py',)``."""
        parts = [p.strip() for p in self.review_file_extensions.split(",") if p.strip()]
        return tuple(p if p.startswith(".") else f".{p}" for p in parts)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor (import-safe, overridable in tests)."""
    return Settings()


settings = get_settings()

__all__ = ["Settings", "settings", "get_settings", "PROJECT_ROOT"]
