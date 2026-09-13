"""Tests for the FastAPI app: webhook handling, read APIs, manual trigger."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import timedelta

import pytest

from reviewbot.api import webhooks
from reviewbot.db.models import Review, ReviewComment, utcnow
from reviewbot.github.models import GitHubFile
from reviewbot.utils.config import settings

WEBHOOK_SECRET = os.environ["GITHUB_WEBHOOK_SECRET"]


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post_webhook(client, payload, event="pull_request", secret=WEBHOOK_SECRET):
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": sign(body, secret),
        "Content-Type": "application/json",
    }
    return client.post("/webhook/github", content=body, headers=headers)


def pr_payload(action="opened", number=42, draft=False, sha="a" * 40):
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "number": number,
            "draft": draft,
            "head": {"sha": sha},
            "title": "Add helper",
        },
        "repository": {"full_name": "octocat/demo"},
        "sender": {"login": "octocat"},
    }


@pytest.fixture
def queued(monkeypatch):
    """Capture background review tasks instead of running them."""
    calls = []
    monkeypatch.setattr(
        webhooks, "process_pr_review", lambda *args, **kwargs: calls.append(args)
    )
    return calls


def seed_review(
    db, repo="octocat/demo", pr_number=1, commit_sha="a" * 40, **overrides
):
    review = Review(
        repo_name=repo,
        pr_number=pr_number,
        commit_sha=commit_sha,
        summary="Two issues found",
        confidence_score=0.9,
        files_reviewed=2,
        bugs=[{"file": "app.py", "line": 4, "description": "Div by zero", "severity": "high"}],
        security_issues=[
            {"file": "app.py", "line": 7, "description": "Shell injection", "severity": "high"}
        ],
        code_smells=[],
        performance_issues=[],
        best_practices=[
            {"file": "app.py", "line": 3, "description": "No type hints", "severity": "low"}
        ],
        **overrides,
    )
    db.add(review)
    db.flush()
    db.add(
        ReviewComment(
            review_id=review.id,
            file_path="app.py",
            line_number=4,
            comment="Guard against zero",
            comment_type="bugs",
            severity="high",
            confidence=0.9,
            posted=1,
        )
    )
    db.commit()
    return review.id


# -- Meta ---------------------------------------------------------------------


def test_root_lists_endpoints(client):
    body = client.get("/").json()

    assert body["name"] == "ReviewBot AI"
    assert body["endpoints"]["webhook"] == "POST /webhook/github"


def test_health_reports_database_and_config(client):
    response = client.get("/health")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "healthy"
    assert body["database"] == "ok"
    # Only booleans, never the secret values themselves.
    assert body["config"] == {
        "github_token": True,
        "webhook_secret": True,
        "anthropic_auth_token": True,
    }


# -- Webhook ------------------------------------------------------------------


def test_webhook_answers_ping(client):
    response = post_webhook(client, {"zen": "Keep it simple", "repository": {}}, event="ping")

    assert response.status_code == 200
    assert response.json()["status"] == "pong"


def test_webhook_rejects_a_bad_signature(client, queued):
    response = post_webhook(client, pr_payload(), secret="wrong-secret")

    assert response.status_code == 401
    assert queued == []


def test_webhook_rejects_a_missing_signature(client, queued):
    response = client.post(
        "/webhook/github",
        content=json.dumps(pr_payload()).encode(),
        headers={"X-GitHub-Event": "pull_request"},
    )

    assert response.status_code == 401
    assert queued == []


def test_webhook_rejects_invalid_json(client):
    body = b"{not json"
    response = client.post(
        "/webhook/github",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body)},
    )

    assert response.status_code == 400


def test_webhook_queues_a_review(client, queued):
    response = post_webhook(client, pr_payload(action="opened", number=7))

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "repo": "octocat/demo",
        "pr_number": 7,
        "action": "opened",
    }
    assert queued == [("octocat/demo", 7, "a" * 40)]


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_webhook_handles_every_reviewable_action(client, queued, action):
    assert post_webhook(client, pr_payload(action=action)).json()["status"] == "accepted"
    assert len(queued) == 1


@pytest.mark.parametrize("action", ["closed", "labeled", "assigned"])
def test_webhook_ignores_other_actions(client, queued, action):
    assert post_webhook(client, pr_payload(action=action)).json()["status"] == "ignored"
    assert queued == []


def test_webhook_ignores_drafts(client, queued):
    response = post_webhook(client, pr_payload(draft=True))

    assert response.json()["reason"] == "draft pull request"
    assert queued == []


def test_webhook_ignores_unrelated_events(client, queued):
    response = post_webhook(client, {"repository": {}}, event="push")

    assert response.json()["status"] == "ignored"
    assert queued == []


def test_webhook_requires_repository_and_number(client, queued):
    payload = pr_payload()
    payload["repository"] = {}
    payload["pull_request"].pop("number")
    payload.pop("number")

    assert post_webhook(client, payload).status_code == 400


def test_webhook_without_a_secret_is_rejected_in_production(monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "app_env", "production")

    assert webhooks.verify_signature(b"{}", None) is False


def test_webhook_without_a_secret_is_allowed_in_development(monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "app_env", "development")

    assert webhooks.verify_signature(b"{}", None) is True


# -- Review pipeline helpers --------------------------------------------------


def make_file(filename, status="modified", patch="@@ -1,1 +1,2 @@\n+a = 1\n"):
    return GitHubFile(
        filename=filename, status=status, additions=1, deletions=0, changes=1, patch=patch
    )


def test_reviewable_files_filters_by_extension_status_and_patch():
    files = [
        make_file("app.py"),
        make_file("README.md"),
        make_file("deleted.py", status="removed"),
        make_file("binary.py", patch=None),
        make_file("pkg/module.py"),
    ]

    kept = [f.filename for f in webhooks.reviewable_files(files)]

    assert kept == ["app.py", "pkg/module.py"]


def test_collect_inline_comments_caps_the_total(monkeypatch, sample_review):
    monkeypatch.setattr(settings, "max_inline_comments", 2)

    comments = webhooks.collect_inline_comments({"file_reviews": [sample_review]})

    assert len(comments) == 2
    # The cap keeps the most severe findings.
    assert {c["severity"] for c in comments} == {"high"}


def test_persist_review_stores_issues_and_comments(sample_review):
    result = {
        "file_reviews": [sample_review],
        "summary": "Summary text",
        "average_confidence": 0.9,
        "bugs": [{**sample_review["bugs"][0], "file": "app.py"}],
        "security": [{**sample_review["security"][0], "file": "app.py"}],
        "smells": [],
        "performance": [],
        "best_practices": [{**sample_review["best_practices"][0], "file": "app.py"}],
    }
    inline = webhooks.collect_inline_comments(result)

    review_id, comment_ids = webhooks.persist_review(
        "octocat/demo", 9, "a" * 40, result, inline
    )

    assert review_id is not None
    assert len(comment_ids) == len(inline)

    webhooks.mark_posted(comment_ids[:1])

    from reviewbot.db.database import SessionLocal

    db = SessionLocal()
    try:
        stored = db.query(Review).filter(Review.id == review_id).one()
        assert stored.pr_number == 9
        assert len(stored.bugs) == 1
        assert stored.bugs[0]["file"] == "app.py"
        assert len(stored.security_issues) == 1
        posted = db.query(ReviewComment).filter(ReviewComment.posted == 1).count()
        assert posted == 1
    finally:
        db.close()


# -- Read APIs ----------------------------------------------------------------


def test_reviews_is_empty_before_any_review(client):
    body = client.get("/api/reviews").json()

    assert body == {"total": 0, "limit": 20, "offset": 0, "reviews": []}


def test_reviews_lists_newest_first(client, db_session):
    older = seed_review(db_session, pr_number=1)
    newer = seed_review(db_session, pr_number=2)

    body = client.get("/api/reviews").json()

    assert body["total"] == 2
    assert [r["id"] for r in body["reviews"]] == [newer, older]
    assert body["reviews"][0]["total_issues"] == 3
    assert body["reviews"][0]["issue_counts"]["bugs"] == 1


def test_reviews_can_be_filtered_and_paged(client, db_session):
    seed_review(db_session, repo="octocat/demo", pr_number=1)
    seed_review(db_session, repo="other/repo", pr_number=2)

    filtered = client.get("/api/reviews", params={"repo": "other/repo"}).json()
    paged = client.get("/api/reviews", params={"limit": 1, "offset": 1}).json()

    assert filtered["total"] == 1
    assert filtered["reviews"][0]["repo_name"] == "other/repo"
    assert paged["total"] == 2
    assert len(paged["reviews"]) == 1


def test_review_detail_includes_issues_and_comments(client, db_session):
    review_id = seed_review(db_session)

    body = client.get(f"/api/reviews/{review_id}").json()

    assert body["id"] == review_id
    assert body["issues"]["bugs"][0]["description"] == "Div by zero"
    assert body["issues"]["security"][0]["file"] == "app.py"
    assert len(body["comments"]) == 1
    assert body["comments"][0]["posted"] is True


def test_review_detail_404s_for_unknown_ids(client):
    assert client.get("/api/reviews/999").status_code == 404
    assert client.get("/api/reviews/999/comments").status_code == 404


def test_review_comments_endpoint(client, db_session):
    review_id = seed_review(db_session)

    body = client.get(f"/api/reviews/{review_id}/comments").json()

    assert body["count"] == 1
    assert body["comments"][0]["file_path"] == "app.py"
    assert body["comments"][0]["line_number"] == 4


def test_repos_endpoint_counts_reviews(client, db_session):
    seed_review(db_session, repo="octocat/demo", pr_number=1)
    seed_review(db_session, repo="octocat/demo", pr_number=2)
    seed_review(db_session, repo="other/repo", pr_number=3)

    repos = client.get("/api/repos").json()["repos"]

    assert repos[0] == {
        "repo_name": "octocat/demo",
        "reviews": 2,
        "last_review": repos[0]["last_review"],
    }
    assert {r["repo_name"] for r in repos} == {"octocat/demo", "other/repo"}


def test_metrics_aggregates_without_summing_ints(client, db_session):
    """The metrics endpoint must count issues across reviews, not sum sums."""
    seed_review(db_session, pr_number=1)
    seed_review(db_session, pr_number=2)

    body = client.get("/api/metrics").json()

    assert body["total_reviews"] == 2
    assert body["total_bugs"] == 2
    assert body["total_security"] == 2
    assert body["total_best_practices"] == 2
    assert body["total_issues"] == 6
    assert body["severity_breakdown"] == {"high": 4, "medium": 0, "low": 2}
    assert body["average_confidence"] == pytest.approx(0.9)
    assert body["total_files_reviewed"] == 4
    assert body["repos_reviewed"] == 1
    assert body["reviews_last_7_days"] == 2
    assert body["issues_per_review"] == pytest.approx(3.0)
    assert body["posted_comments"] == 2


def test_metrics_on_an_empty_database(client):
    body = client.get("/api/metrics").json()

    assert body["total_reviews"] == 0
    assert body["average_confidence"] == 0.0
    assert body["issues_per_review"] == 0.0


def test_metrics_only_counts_recent_reviews_as_recent(client, db_session):
    seed_review(db_session, pr_number=1, created_at=utcnow() - timedelta(days=30))

    body = client.get("/api/metrics").json()

    assert body["total_reviews"] == 1
    assert body["reviews_last_7_days"] == 0


# -- Manual trigger -----------------------------------------------------------


def test_manual_review_requires_the_token(client):
    response = client.post("/api/review", json={"repo": "octocat/demo", "pr_number": 7})

    assert response.status_code == 401


def test_manual_review_rejects_a_wrong_token(client):
    response = client.post(
        "/api/review",
        json={"repo": "octocat/demo", "pr_number": 7},
        headers={"X-ReviewBot-Token": "nope"},
    )

    assert response.status_code == 401


def test_manual_review_queues_the_task(client, monkeypatch):
    from reviewbot.api import routes

    calls = []
    monkeypatch.setattr(routes, "process_pr_review", lambda *args: calls.append(args))

    response = client.post(
        "/api/review",
        json={"repo": "octocat/demo", "pr_number": 7, "commit_sha": "b" * 40},
        headers={"X-ReviewBot-Token": WEBHOOK_SECRET},
    )

    assert response.status_code == 202
    assert calls == [("octocat/demo", 7, "b" * 40)]


def test_manual_review_validates_the_repository_name(client):
    response = client.post(
        "/api/review",
        json={"repo": "not-a-full-name", "pr_number": 7},
        headers={"X-ReviewBot-Token": WEBHOOK_SECRET},
    )

    assert response.status_code == 400


def test_manual_review_is_disabled_in_production_without_a_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "app_env", "production")

    response = client.post("/api/review", json={"repo": "octocat/demo", "pr_number": 7})

    assert response.status_code == 503


# -- Background pipeline ------------------------------------------------------


class FakeGitHub:
    """Records what would have been sent to GitHub."""

    def __init__(self, files=None, fail=False, compare=None, config=None):
        self.files = files if files is not None else [make_file("app.py")]
        self.fail = fail
        self.compare = compare  # ComparisonResult, exception, or None (no prior sha)
        self.config = config  # .reviewbot.yaml text, or None
        self.summary_comments = []
        self.inline_comments = []
        self.compare_calls = []

    def get_pr_files(self, repo_name, pr_number):
        if self.fail:
            raise RuntimeError("GitHub is down")
        return self.files

    def get_pr(self, repo_name, pr_number):
        from types import SimpleNamespace

        return SimpleNamespace(
            head=SimpleNamespace(sha="c" * 40),
            base=SimpleNamespace(sha="d" * 40),
            title="Add helper",
        )

    def get_contents(self, repo_name, path, ref=None):
        return self.config

    def compare_commits(self, repo_name, base, head):
        self.compare_calls.append((base, head))
        if isinstance(self.compare, Exception):
            raise self.compare
        return self.compare

    def post_comment(self, repo_name, pr_number, body):
        self.summary_comments.append(body)

    def post_inline_comments(self, repo_name, pr_number, commit_sha, comments):
        for comment in comments:
            comment["posted"] = True
        self.inline_comments.extend(comments)
        return len(comments)


def aggregate_from(sample_review):
    """A review_multiple_files-shaped result built from one file review."""
    tagged = {
        key: [{**issue, "file": "app.py"} for issue in sample_review[key]]
        for key in ("bugs", "security", "smells", "performance", "best_practices")
    }
    return {
        "file_reviews": [sample_review],
        "skipped_files": [],
        "summary": "## AI Code Review\n\nTwo issues found",
        "average_confidence": 0.9,
        "total_issues": sum(len(v) for v in tagged.values()),
        **tagged,
        **{f"total_{key}": len(value) for key, value in tagged.items()},
    }


@pytest.fixture
def pipeline(monkeypatch, sample_review):
    """Patch GitHub and the reviewer, and hand back the fake GitHub client."""

    def _install(files=None, fail=False, compare=None, config=None):
        fake = FakeGitHub(files=files, fail=fail, compare=compare, config=config)
        monkeypatch.setattr(webhooks, "GitHubClient", lambda *a, **kw: fake)

        reviewer_state = {}

        class FakeReviewer:
            def __init__(self, *args, **kwargs):
                pass

            def review_multiple_files(self, files, context=None):
                self.received = files
                reviewer_state["files"] = files
                reviewer_state["context"] = context
                return aggregate_from(sample_review)

        reviewer_state["reviewer"] = FakeReviewer
        monkeypatch.setattr(webhooks, "CodeReviewer", FakeReviewer)
        fake.reviewer_state = reviewer_state
        return fake

    return _install


def test_process_pr_review_stores_and_posts(pipeline):
    fake = pipeline()

    review_id = webhooks.process_pr_review("octocat/demo", 5)

    assert review_id is not None
    assert "AI Code Review" in fake.summary_comments[0]
    assert [c["line"] for c in fake.inline_comments] == [4, 7, 3]

    from reviewbot.db.database import SessionLocal

    db = SessionLocal()
    try:
        stored = db.query(Review).filter(Review.id == review_id).one()
        assert stored.commit_sha == "c" * 40
        assert stored.files_reviewed == 1
        assert db.query(ReviewComment).filter(ReviewComment.posted == 1).count() == 3
    finally:
        db.close()


def test_process_pr_review_skips_when_nothing_is_reviewable(pipeline):
    fake = pipeline(files=[make_file("README.md")])

    assert webhooks.process_pr_review("octocat/demo", 5) is None
    assert fake.summary_comments == []


def test_process_pr_review_swallows_errors(pipeline):
    pipeline(fail=True)

    assert webhooks.process_pr_review("octocat/demo", 5) is None


# -- Incremental reviews -------------------------------------------------------


def fake_comparison(files, behind_by=0):
    from reviewbot.github.models import ComparisonResult

    return ComparisonResult(
        files=files, total_commits=1, ahead_by=1, behind_by=behind_by
    )


def test_get_last_reviewed_sha_returns_the_most_recent(db_session):
    seed_review(db_session, pr_number=5, commit_sha="o" * 40)
    seed_review(db_session, pr_number=5, commit_sha="n" * 40)

    assert webhooks.get_last_reviewed_sha("octocat/demo", 5) == "n" * 40
    assert webhooks.get_last_reviewed_sha("other/repo", 5) is None
    assert webhooks.get_last_reviewed_sha("octocat/demo", 6) is None


def test_get_last_reviewed_sha_prefers_the_newest_row(db_session):
    older = seed_review(db_session, pr_number=5, commit_sha="a" * 40)
    newer = seed_review(db_session, pr_number=5, commit_sha="b" * 40)
    # created_at defaults to utcnow for both; break the tie by id order.
    assert older < newer
    assert webhooks.get_last_reviewed_sha("octocat/demo", 5) == "b" * 40


def test_incremental_review_uses_the_delta_since_the_last_review(pipeline, db_session):
    seed_review(db_session, pr_number=5, commit_sha="a" * 40)
    fake = pipeline(
        files=[make_file("unchanged.py")],
        compare=fake_comparison([make_file("new.py")]),
    )

    review_id = webhooks.process_pr_review("octocat/demo", 5, "c" * 40, incremental=True)

    assert review_id is not None
    # Only the delta file is reviewed, not the full PR file list.
    assert [f["filename"] for f in fake.reviewer_state["files"]] == ["new.py"]
    assert fake.compare_calls == [("a" * 40, "c" * 40)]
    # The review context marks the range and the DB stores it.
    context = fake.reviewer_state["context"]
    assert context["incremental"] is True
    assert context["base_sha"] == "a" * 40
    assert context["head_sha"] == "c" * 40

    from reviewbot.db.database import SessionLocal

    db = SessionLocal()
    try:
        stored = db.query(Review).filter(Review.id == review_id).one()
        assert stored.commit_sha == "c" * 40
        assert stored.base_commit_sha == "a" * 40
    finally:
        db.close()


def test_incremental_review_falls_back_to_a_full_review_on_compare_error(pipeline, db_session):
    seed_review(db_session, pr_number=5, commit_sha="a" * 40)
    fake = pipeline(
        files=[make_file("full.py")],
        compare=RuntimeError("compare limit exceeded"),
    )

    review_id = webhooks.process_pr_review("octocat/demo", 5, "c" * 40, incremental=True)

    assert review_id is not None
    assert [f["filename"] for f in fake.reviewer_state["files"]] == ["full.py"]
    assert fake.reviewer_state["context"]["incremental"] is False


def test_incremental_review_falls_back_when_history_diverged(pipeline, db_session):
    seed_review(db_session, pr_number=5, commit_sha="a" * 40)
    fake = pipeline(
        files=[make_file("full.py")],
        compare=fake_comparison([make_file("new.py")], behind_by=2),
    )

    review_id = webhooks.process_pr_review("octocat/demo", 5, "c" * 40, incremental=True)

    assert review_id is not None
    assert [f["filename"] for f in fake.reviewer_state["files"]] == ["full.py"]


def test_incremental_review_without_a_prior_review_is_full(pipeline):
    fake = pipeline(files=[make_file("full.py")])

    review_id = webhooks.process_pr_review("octocat/demo", 5, incremental=True)

    assert review_id is not None
    assert fake.compare_calls == []
    assert fake.reviewer_state["context"]["incremental"] is False


def test_incremental_review_skips_an_already_reviewed_head(pipeline, db_session):
    seed_review(db_session, pr_number=5, commit_sha="c" * 40)
    fake = pipeline()

    assert webhooks.process_pr_review("octocat/demo", 5, "c" * 40, incremental=True) is None
    assert fake.compare_calls == []
    assert fake.summary_comments == []


def test_webhook_passes_incremental_for_synchronize_only(client, monkeypatch):
    calls = []

    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    monkeypatch.setattr(webhooks, "process_pr_review", capture)

    post_webhook(client, pr_payload(action="synchronize"))
    post_webhook(client, pr_payload(action="opened"))

    assert calls[0][1] == {"incremental": True}
    assert calls[1][1] == {"incremental": False}


# -- Per-repo config -----------------------------------------------------------


def test_repo_config_excludes_paths_from_the_review(pipeline):
    fake = pipeline(
        files=[make_file("app.py"), make_file("generated.py")],
        config="exclude:\n  - generated.py\n",
    )

    review_id = webhooks.process_pr_review("octocat/demo", 5)

    assert review_id is not None
    assert [f["filename"] for f in fake.reviewer_state["files"]] == ["app.py"]


def test_repo_config_can_disable_the_bot_entirely(pipeline, db_session):
    fake = pipeline(config="enabled: false\n")

    assert webhooks.process_pr_review("octocat/demo", 5) is None
    assert fake.summary_comments == []
    assert fake.inline_comments == []
    assert db_session.query(Review).count() == 0


def test_repo_config_overrides_extensions_and_inline_cap(pipeline):
    fake = pipeline(
        files=[make_file("app.py"), make_file("helper.js")],
        config="extensions: [js]\nmax_inline_comments: 0\n",
    )

    review_id = webhooks.process_pr_review("octocat/demo", 5)

    assert review_id is not None
    assert [f["filename"] for f in fake.reviewer_state["files"]] == ["helper.js"]
    # max_inline_comments: 0 means no inline comments at all.
    assert fake.inline_comments == []


def test_repo_config_min_severity_gates_inline_comments(pipeline):
    fake = pipeline(config="min_severity: high\n")

    review_id = webhooks.process_pr_review("octocat/demo", 5)

    assert review_id is not None
    # sample_review has high, high and low findings; only the highs survive.
    assert {c["severity"] for c in fake.inline_comments} == {"high"}


def test_collect_inline_comments_applies_the_severity_threshold(sample_review):
    from reviewbot.utils.repo_config import EffectiveReviewConfig

    cfg = EffectiveReviewConfig.from_repo_config(None)
    cfg.min_severity = "high"

    comments = webhooks.collect_inline_comments({"file_reviews": [sample_review]}, cfg)

    assert {c["severity"] for c in comments} == {"high"}


def test_malformed_repo_config_falls_back_to_globals(pipeline):
    fake = pipeline(files=[make_file("app.py")], config="exclude: [unclosed")

    review_id = webhooks.process_pr_review("octocat/demo", 5)

    assert review_id is not None
    assert [f["filename"] for f in fake.reviewer_state["files"]] == ["app.py"]


def test_persist_review_stores_the_walkthrough(sample_review):
    result = {
        "file_reviews": [sample_review],
        "summary": "Summary text",
        "walkthrough": "Adds a division helper and a command runner.",
        "average_confidence": 0.9,
        "bugs": [{**sample_review["bugs"][0], "file": "app.py"}],
        "security": [],
        "smells": [],
        "performance": [],
        "best_practices": [],
    }

    review_id, _ = webhooks.persist_review(
        "octocat/demo",
        9,
        "a" * 40,
        result,
        [],
        base_commit_sha="9" * 40,
    )

    from reviewbot.db.database import SessionLocal

    db = SessionLocal()
    try:
        stored = db.query(Review).filter(Review.id == review_id).one()
        assert stored.walkthrough == "Adds a division helper and a command runner."
        assert stored.base_commit_sha == "9" * 40
    finally:
        db.close()
