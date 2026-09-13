"""Tests for the PyGithub wrapper. No network access: the client is faked."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

from reviewbot.github.client import GitHubClient
from reviewbot.utils.config import settings


def fake_file(filename="app.py", status="modified", patch="@@ -1,1 +1,2 @@\n+a = 1\n"):
    return SimpleNamespace(
        filename=filename,
        status=status,
        additions=1,
        deletions=0,
        changes=1,
        patch=patch,
        raw_url=f"https://raw/{filename}",
        blob_url=f"https://blob/{filename}",
    )


def fake_pr(number=42, files=None):
    pr = MagicMock()
    pr.number = number
    pr.title = "Add division helper"
    pr.body = "Adds a helper"
    pr.state = "open"
    pr.user = SimpleNamespace(login="octocat")
    pr.head = SimpleNamespace(sha="a" * 40)
    pr.base = SimpleNamespace(sha="b" * 40)
    pr.created_at = datetime(2026, 9, 1, 12, 0, 0)
    pr.updated_at = datetime(2026, 9, 2, 12, 0, 0)
    pr.html_url = "https://github.com/octocat/demo/pull/42"
    pr.get_files.return_value = files if files is not None else [fake_file()]
    return pr


def build_client(pr=None, commit=None):
    """A GitHubClient whose PyGithub handle is a MagicMock."""
    pr = pr if pr is not None else fake_pr()
    commit = commit if commit is not None else MagicMock(name="commit")

    repo = MagicMock()
    repo.get_pull.return_value = pr
    repo.get_commit.return_value = commit

    gh = MagicMock()
    gh.get_repo.return_value = repo

    client = GitHubClient(token="test-token")
    client._gh = gh
    return client, gh, repo, pr, commit


def test_gh_property_requires_a_token(monkeypatch):
    monkeypatch.setattr(settings, "github_token", "")
    client = GitHubClient()

    with pytest.raises(ValueError, match="GitHub token not provided"):
        _ = client.gh


def test_importing_the_client_does_not_need_credentials(monkeypatch):
    """Constructing the client must never touch the network or raise."""
    monkeypatch.setattr(settings, "github_token", "")

    assert GitHubClient()._gh is None


def test_get_pr_files_maps_github_fields():
    client, _, _, _, _ = build_client(pr=fake_pr(files=[fake_file("a.py"), fake_file("b.py")]))

    files = client.get_pr_files("octocat/demo", 42)

    assert [f.filename for f in files] == ["a.py", "b.py"]
    assert files[0].status == "modified"
    assert files[0].patch.startswith("@@")
    assert files[0].blob_url == "https://blob/a.py"


def test_get_pr_diff_concatenates_patches_and_skips_empty_ones():
    files = [
        fake_file("a.py", patch="@@ -1,1 +1,2 @@\n+a = 1\n"),
        fake_file("binary.png", patch=None),
        fake_file("b.py", patch="@@ -1,1 +1,2 @@\n+b = 2\n"),
    ]
    client, _, _, _, _ = build_client(pr=fake_pr(files=files))

    diff = client.get_pr_diff("octocat/demo", 42)

    assert "--- a.py" in diff and "--- b.py" in diff
    assert "binary.png" not in diff
    assert "+a = 1" in diff


def test_get_pr_summary_normalizes_the_pull_request():
    client, _, _, _, _ = build_client()

    summary = client.get_pr_summary("octocat/demo", 42)

    assert summary.number == 42
    assert summary.user == "octocat"
    assert summary.repo == "octocat/demo"
    assert summary.head_sha == "a" * 40
    assert summary.html_url.endswith("/pull/42")


def test_post_comment_creates_an_issue_comment():
    client, _, _, pr, _ = build_client()

    client.post_comment("octocat/demo", 42, "Nice work")

    pr.create_issue_comment.assert_called_once_with("Nice work")


def test_post_inline_comment_targets_the_right_hand_side():
    client, _, repo, pr, commit = build_client()

    client.post_inline_comment("octocat/demo", 42, "c" * 40, "app.py", 4, "Careful")

    repo.get_commit.assert_called_once_with("c" * 40)
    pr.create_review_comment.assert_called_once_with(
        body="Careful", commit=commit, path="app.py", line=4, side="RIGHT"
    )


def test_post_inline_comments_reuses_the_pr_and_commit_lookup():
    client, _, repo, pr, _ = build_client()
    comments = [
        {"file": "app.py", "line": 1, "body": "one"},
        {"file": "app.py", "line": 2, "body": "two"},
        {"file": "app.py", "line": 3, "body": "three"},
    ]

    posted = client.post_inline_comments("octocat/demo", 42, "c" * 40, comments)

    assert posted == 3
    assert pr.create_review_comment.call_count == 3
    # One PR fetch and one commit fetch, no matter how many comments.
    assert repo.get_pull.call_count == 1
    assert repo.get_commit.call_count == 1
    assert all(c["posted"] for c in comments)


def test_post_inline_comments_skips_individual_failures():
    client, _, _, pr, _ = build_client()
    pr.create_review_comment.side_effect = [
        MagicMock(),
        GithubException(422, {"message": "line must be part of the diff"}, {}),
        MagicMock(),
    ]
    comments = [
        {"file": "app.py", "line": 1, "body": "one"},
        {"file": "app.py", "line": 999, "body": "stale line"},
        {"file": "app.py", "line": 3, "body": "three"},
    ]

    posted = client.post_inline_comments("octocat/demo", 42, "c" * 40, comments)

    assert posted == 2
    assert [c["posted"] for c in comments] == [True, False, True]


def test_post_inline_comments_with_nothing_to_post_makes_no_calls():
    client, _, repo, _, _ = build_client()

    assert client.post_inline_comments("octocat/demo", 42, "c" * 40, []) == 0
    repo.get_pull.assert_not_called()


def test_github_errors_propagate():
    client, gh, _, _, _ = build_client()
    gh.get_repo.side_effect = GithubException(404, {"message": "Not Found"}, {})

    with pytest.raises(GithubException):
        client.get_repo("octocat/missing")


# -- compare_commits -----------------------------------------------------------


def fake_comparison(files=None, ahead_by=1, behind_by=0):
    comparison = MagicMock()
    comparison.files = files if files is not None else [fake_file("delta.py")]
    comparison.total_commits = ahead_by
    comparison.ahead_by = ahead_by
    comparison.behind_by = behind_by
    return comparison


def test_compare_commits_maps_files_and_metadata():
    client, _, repo, _, _ = build_client()
    repo.compare.return_value = fake_comparison(
        files=[fake_file("a.py"), fake_file("b.py", status="added")], ahead_by=3
    )

    result = client.compare_commits("octocat/demo", "b" * 40, "c" * 40)

    repo.compare.assert_called_once_with("b" * 40, "c" * 40)
    assert [f.filename for f in result.files] == ["a.py", "b.py"]
    assert result.files[0].patch.startswith("@@")
    assert result.ahead_by == 3
    assert result.behind_by == 0


def test_compare_commits_propagates_github_errors():
    client, _, repo, _, _ = build_client()
    repo.compare.side_effect = GithubException(400, {"message": "too many commits"}, {})

    with pytest.raises(GithubException):
        client.compare_commits("octocat/demo", "b" * 40, "c" * 40)


# -- get_contents --------------------------------------------------------------


def fake_content_file(text=b"max_inline_comments: 3\n", type_="file"):
    content = MagicMock()
    content.type = type_
    content.decoded_content = text
    return content


def test_get_contents_decodes_file_text():
    client, _, repo, _, _ = build_client()
    repo.get_contents.return_value = fake_content_file()

    text = client.get_contents("octocat/demo", ".reviewbot.yaml", ref="d" * 40)

    repo.get_contents.assert_called_once_with(".reviewbot.yaml", ref="d" * 40)
    assert text == "max_inline_comments: 3\n"


def test_get_contents_returns_none_on_404():
    client, _, repo, _, _ = build_client()
    repo.get_contents.side_effect = GithubException(404, {"message": "Not Found"}, {})

    assert client.get_contents("octocat/demo", ".reviewbot.yaml") is None


def test_get_contents_returns_none_for_directories():
    client, _, repo, _, _ = build_client()
    repo.get_contents.return_value = [fake_content_file(), fake_content_file()]

    assert client.get_contents("octocat/demo", "docs") is None


def test_get_contents_returns_none_for_non_file_types():
    client, _, repo, _, _ = build_client()
    repo.get_contents.return_value = fake_content_file(type_="symlink")

    assert client.get_contents("octocat/demo", "link") is None


def test_get_contents_returns_none_for_undecodable_content():
    client, _, repo, _, _ = build_client()
    repo.get_contents.return_value = fake_content_file(text=b"\xff\xfe\x00")

    assert client.get_contents("octocat/demo", "image.bin") is None


def test_get_contents_logs_and_returns_none_on_other_errors():
    client, _, repo, _, _ = build_client()
    repo.get_contents.side_effect = GithubException(403, {"message": "rate limited"}, {})

    assert client.get_contents("octocat/demo", ".reviewbot.yaml") is None
