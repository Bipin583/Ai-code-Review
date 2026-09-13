"""Tests for the per-repo .reviewbot.yaml config: parsing, merging, caching."""

from __future__ import annotations

import pytest

from reviewbot.utils.config import settings
from reviewbot.utils.repo_config import (
    EffectiveReviewConfig,
    RepoConfig,
    clear_config_cache,
    get_repo_config,
    parse_repo_config,
)


class FakeContentClient:
    """Stands in for GitHubClient.get_contents, counting fetches."""

    def __init__(self, content=None):
        self.content = content
        self.calls = []

    def get_contents(self, repo_name, path, ref=None):
        self.calls.append((repo_name, path, ref))
        return self.content


# -- Parsing -------------------------------------------------------------------


def test_parse_repo_config_accepts_a_valid_file():
    config = parse_repo_config(
        "include:\n  - src/**\nexclude:\n  - tests/**\nmin_severity: medium\n"
    )

    assert config is not None
    assert config.include == ["src/**"]
    assert config.exclude == ["tests/**"]
    assert config.min_severity == "medium"
    assert config.enabled is True


def test_parse_repo_config_normalizes_extensions_without_dots():
    config = parse_repo_config("extensions: [py, .js]")

    assert config.extensions == [".py", ".js"]


def test_parse_repo_config_returns_none_for_empty_content():
    assert parse_repo_config("") is None
    assert parse_repo_config("# just a comment\n") is None


def test_parse_repo_config_returns_none_for_malformed_yaml():
    assert parse_repo_config("include: [unclosed") is None


def test_parse_repo_config_returns_none_for_non_mapping_root():
    assert parse_repo_config("- just\n- a\n- list\n") is None


def test_parse_repo_config_returns_none_for_invalid_values():
    assert parse_repo_config("min_severity: critical") is None
    assert parse_repo_config("max_inline_comments: -1") is None


def test_parse_repo_config_warns_and_ignores_unknown_keys(caplog):
    with caplog.at_level("WARNING"):
        config = parse_repo_config("review_depth: deep\nmax_inline_comments: 3")

    assert config is not None
    assert config.max_inline_comments == 3
    assert "review_depth" in caplog.text


# -- Path matching -------------------------------------------------------------


@pytest.mark.parametrize(
    "config,filename,expected",
    [
        ("{}", "app.py", True),
        ("exclude: [app.py]", "app.py", False),
        ("exclude: [app.py]", "other.py", True),
        ("include: [src/**]", "src/deep/app.py", True),
        ("include: [src/**]", "app.py", False),
        ("include: ['*.py']", "a/b/c.py", True),  # fnmatch's * crosses /
        ("exclude: [tests/**]", "tests/test_x.py", False),
    ],
)
def test_matches_path(config, filename, expected):
    assert parse_repo_config(config).matches_path(filename) is expected


# -- Effective config merge ----------------------------------------------------


def test_effective_config_without_repo_config_uses_globals(monkeypatch):
    monkeypatch.setattr(settings, "max_inline_comments", 7)

    cfg = EffectiveReviewConfig.from_repo_config(None)

    assert cfg.max_inline_comments == 7
    assert cfg.extensions == settings.review_extensions
    assert cfg.include is None
    assert cfg.exclude == ()
    assert cfg.min_severity == "low"
    assert cfg.enabled is True


def test_effective_config_applies_repo_overrides():
    repo = parse_repo_config(
        "extensions: [js]\nmax_inline_comments: 2\nmin_severity: high\n"
        "enabled: false\nexclude: [vendor/**]\n"
    )

    cfg = EffectiveReviewConfig.from_repo_config(repo)

    assert cfg.extensions == (".js",)
    assert cfg.max_inline_comments == 2
    assert cfg.min_severity == "high"
    assert cfg.enabled is False
    assert cfg.exclude == ("vendor/**",)
    # Cost guardrails stay global-only.
    assert cfg.max_files_per_review == settings.max_files_per_review
    assert cfg.max_diff_chars == settings.max_diff_chars


def test_effective_config_reads_settings_at_build_time(monkeypatch):
    """Building the config after a settings change picks up the new value."""
    monkeypatch.setattr(settings, "max_inline_comments", 99)

    cfg = EffectiveReviewConfig.from_repo_config(None)

    assert cfg.max_inline_comments == 99


def test_is_reviewable_path_combines_extensions_and_globs():
    cfg = EffectiveReviewConfig.from_repo_config(
        parse_repo_config("include: [src/**]\nexclude: [src/legacy/**]")
    )

    assert cfg.is_reviewable_path("src/app.py")
    assert not cfg.is_reviewable_path("src/legacy/app.py")
    assert not cfg.is_reviewable_path("app.py")
    assert not cfg.is_reviewable_path("src/app.js")


# -- Fetch + cache -------------------------------------------------------------


def test_get_repo_config_fetches_and_caches():
    clear_config_cache()
    client = FakeContentClient("max_inline_comments: 3\n")

    first = get_repo_config(client, "octocat/demo", ref="d" * 40)
    second = get_repo_config(client, "octocat/demo", ref="d" * 40)

    assert first is second
    assert first.max_inline_comments == 3
    assert len(client.calls) == 1


def test_get_repo_config_refetches_for_a_different_ref():
    clear_config_cache()
    client = FakeContentClient("enabled: false\n")

    get_repo_config(client, "octocat/demo", ref="d" * 40)
    get_repo_config(client, "octocat/demo", ref="e" * 40)

    assert len(client.calls) == 2


def test_get_repo_config_returns_none_when_no_file():
    clear_config_cache()
    client = FakeContentClient(None)

    assert get_repo_config(client, "octocat/demo", ref="d" * 40) is None


def test_get_repo_config_falls_back_to_none_on_malformed_file():
    clear_config_cache()
    client = FakeContentClient("not: [valid")

    assert get_repo_config(client, "octocat/demo", ref="d" * 40) is None


def test_repo_config_model_rejects_bad_severity():
    with pytest.raises(ValueError):
        RepoConfig(min_severity="extreme")
