"""Tests for diff annotation, comment rendering and the reviewer itself."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from reviewbot.llm.parser import (
    annotate_diff,
    format_inline_comments,
    format_pr_comment,
    get_severity_badge,
    strip_code_fences,
)
from reviewbot.llm.reviewer import CodeReviewer
from reviewbot.utils.config import settings


class FakeMessages:
    """Stands in for ``client.messages``."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0) if self.responses else ""
        if isinstance(item, Exception):
            raise item
        content = item if isinstance(item, list) else [
            SimpleNamespace(type="text", text=item)
        ]
        return SimpleNamespace(content=content)


def fake_client(*responses):
    messages = FakeMessages(responses)
    client = SimpleNamespace(messages=messages)
    return client, messages


def model_payload(**overrides):
    payload = {
        "bugs": [
            {
                "line": 4,
                "description": "Division by zero",
                "severity": "high",
                "suggestion": "Guard against b == 0",
            }
        ],
        "security": [],
        "smells": [],
        "performance": [],
        "best_practices": [],
        "summary": "One bug found",
        "confidence": 0.85,
    }
    payload.update(overrides)
    return json.dumps(payload)


# -- Diff annotation ----------------------------------------------------------


def test_annotate_diff_numbers_new_file_lines(sample_diff):
    annotated, valid = annotate_diff(sample_diff)

    assert "     1   | import os" in annotated
    # Added lines are numbered and commentable.
    assert 8 in valid
    # The hunk header itself is not commentable.
    assert 0 not in valid
    assert min(valid) == 1


def test_annotate_diff_marks_removed_lines_without_numbers():
    annotated, valid = annotate_diff("@@ -1,2 +1,2 @@\n-old = 1\n+new = 1\n")
    removed = [line for line in annotated.splitlines() if " - | " in line]

    assert removed and removed[0].strip().startswith("- | old = 1")
    assert valid == {1}


def test_annotate_diff_handles_empty_patch():
    assert annotate_diff("") == ("", set())


def test_annotate_diff_tracks_multiple_hunks():
    patch = "@@ -1,1 +1,1 @@\n+a = 1\n@@ -10,1 +20,2 @@\n context\n+b = 2\n"
    _, valid = annotate_diff(patch)

    assert valid == {1, 20, 21}


# -- Response cleanup ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"bugs": []}',
        '```json\n{"bugs": []}\n```',
        '```\n{"bugs": []}\n```',
        'Here is the review:\n{"bugs": []}\nHope that helps!',
    ],
)
def test_strip_code_fences_yields_parsable_json(raw):
    assert json.loads(strip_code_fences(raw)) == {"bugs": []}


def test_get_severity_badge():
    assert "High" in get_severity_badge("high")
    assert "Medium" in get_severity_badge("MEDIUM")
    assert "Low" in get_severity_badge("low")
    assert "Medium" in get_severity_badge("nonsense")


# -- Inline comment rendering -------------------------------------------------


def test_format_inline_comments_orders_by_severity(sample_review):
    comments = format_inline_comments(sample_review)

    assert [c["severity"] for c in comments] == ["high", "high", "low"]
    assert all(c["file"] == "app.py" for c in comments)
    assert "Suggestion" in comments[0]["body"]


def test_format_inline_comments_drops_lines_outside_the_diff(sample_review):
    sample_review["bugs"][0]["line"] = 999

    lines = [c["line"] for c in format_inline_comments(sample_review)]

    assert 999 not in lines


def test_format_inline_comments_drops_missing_line_numbers(sample_review):
    sample_review["bugs"][0]["line"] = None

    assert len(format_inline_comments(sample_review)) == 2


def test_format_inline_comments_dedupes_identical_issues(sample_review):
    sample_review["bugs"].append(dict(sample_review["bugs"][0]))

    assert len(format_inline_comments(sample_review)) == 3


def test_format_pr_comment_uses_summary():
    assert format_pr_comment({"summary": "All good"}) == "All good"
    assert format_pr_comment({}) == "No summary available"


# -- CodeReviewer -------------------------------------------------------------


def test_review_file_parses_model_response(sample_diff):
    client, completions = fake_client(model_payload())
    reviewer = CodeReviewer(client=client)

    review = reviewer.review_file("app.py", sample_diff)

    assert review["filename"] == "app.py"
    assert len(review["bugs"]) == 1
    assert review["bugs"][0]["severity"] == "high"
    assert review["confidence"] == pytest.approx(0.85)
    assert review["valid_lines"]
    # The prompt carries annotated line numbers, not the raw patch.
    sent = completions.calls[0]["messages"][0]["content"]
    assert "1   | import os" in sent
    assert completions.calls[0]["model"] == settings.anthropic_model
    assert "expert Python code reviewer" in completions.calls[0]["system"]


def test_review_file_skips_empty_diff():
    client, completions = fake_client(model_payload())
    reviewer = CodeReviewer(client=client)

    review = reviewer.review_file("app.py", "")

    assert review["bugs"] == []
    assert "No reviewable changes" in review["summary"]
    assert completions.calls == []


def test_review_file_survives_invalid_json(sample_diff):
    client, _ = fake_client("not json at all")
    reviewer = CodeReviewer(client=client)

    review = reviewer.review_file("app.py", sample_diff)

    assert review["bugs"] == []
    assert review["confidence"] == 0.0
    assert "not valid JSON" in review["summary"]


def test_review_file_survives_api_errors(sample_diff, monkeypatch):
    monkeypatch.setattr("reviewbot.llm.reviewer.time.sleep", lambda *_: None)
    client, _ = fake_client(RuntimeError("boom"), RuntimeError("boom again"))
    reviewer = CodeReviewer(client=client)

    review = reviewer.review_file("app.py", sample_diff)

    assert review["confidence"] == 0.0
    assert "Error during review" in review["summary"]
    assert review["valid_lines"]


def test_review_file_retries_then_succeeds(sample_diff, monkeypatch):
    monkeypatch.setattr("reviewbot.llm.reviewer.time.sleep", lambda *_: None)
    client, completions = fake_client(RuntimeError("rate limited"), model_payload())
    reviewer = CodeReviewer(client=client)

    review = reviewer.review_file("app.py", sample_diff)

    assert len(completions.calls) == 2
    assert len(review["bugs"]) == 1


def test_review_file_combines_text_blocks(sample_diff):
    payload = model_payload()
    split_at = len(payload) // 2
    blocks = [
        SimpleNamespace(type="text", text=payload[:split_at]),
        SimpleNamespace(type="tool_use", text="ignored"),
        SimpleNamespace(type="text", text=payload[split_at:]),
    ]
    client, messages = fake_client(blocks)
    reviewer = CodeReviewer(client=client)

    review = reviewer.review_file("app.py", sample_diff)

    assert len(messages.calls) == 1
    assert review["summary"] == "One bug found"


def test_review_file_truncates_oversized_diffs(monkeypatch):
    monkeypatch.setattr(settings, "max_diff_chars", 80)
    client, completions = fake_client(model_payload())
    reviewer = CodeReviewer(client=client)

    big_diff = "@@ -1,1 +1,200 @@\n" + "\n".join(f"+line_{i} = {i}" for i in range(200))
    reviewer.review_file("big.py", big_diff)

    assert "diff truncated for length" in completions.calls[0]["messages"][0]["content"]


def test_normalize_clamps_severity_confidence_and_lines():
    payload = json.dumps(
        {
            "bugs": [
                {"line": "4", "description": "coerced string line", "severity": "CRITICAL"},
                {"line": -3, "description": "bad line number"},
                {"line": 2, "description": ""},
                "not a dict",
            ],
            "security": "not a list",
            "confidence": 7.5,
        }
    )
    client, _ = fake_client(payload)
    reviewer = CodeReviewer(client=client)

    review = reviewer.review_file("app.py", "@@ -1,1 +1,5 @@\n+a = 1\n+b = 2\n")

    assert review["confidence"] == 1.0
    assert review["security"] == []
    assert [b["line"] for b in review["bugs"]] == [4, None]
    assert review["bugs"][0]["severity"] == "medium"


def test_review_multiple_files_aggregates_flat_issue_lists(sample_diff):
    client, _ = fake_client(
        model_payload(),
        model_payload(
            bugs=[],
            security=[
                {
                    "line": 8,
                    "description": "Shell injection",
                    "severity": "high",
                    "suggestion": "Use subprocess",
                }
            ],
            summary="Security issue",
            confidence=0.75,
        ),
    )
    reviewer = CodeReviewer(client=client)

    result = reviewer.review_multiple_files(
        [
            {"filename": "a.py", "diff": sample_diff},
            {"filename": "b.py", "diff": sample_diff},
        ]
    )

    assert len(result["file_reviews"]) == 2
    assert result["total_bugs"] == 1
    assert result["total_security"] == 1
    assert result["total_issues"] == 2
    # Every flattened issue remembers the file it came from.
    assert result["bugs"][0]["file"] == "a.py"
    assert result["security"][0]["file"] == "b.py"
    assert result["average_confidence"] == pytest.approx(0.8)
    assert "AI Code Review" in result["summary"]
    assert "Bugs | 1" in result["summary"]


def test_review_multiple_files_handles_no_files():
    reviewer = CodeReviewer(client=object())

    result = reviewer.review_multiple_files([])

    assert result["total_issues"] == 0
    assert result["file_reviews"] == []
    assert result["summary"] == "No files to review"


def test_review_multiple_files_caps_file_count(monkeypatch, sample_diff):
    monkeypatch.setattr(settings, "max_files_per_review", 1)
    client, completions = fake_client(model_payload(), "Adds a division helper.")
    reviewer = CodeReviewer(client=client)

    result = reviewer.review_multiple_files(
        [
            {"filename": "a.py", "diff": sample_diff},
            {"filename": "b.py", "diff": sample_diff},
        ]
    )

    # One file review, then one walkthrough call for the surviving file.
    review_calls = [
        c for c in completions.calls if "Review this Python" in c["messages"][0]["content"]
    ]
    assert len(review_calls) == 1
    assert result["skipped_files"] == ["b.py"]
    assert "exceeded the per-PR review limit" in result["summary"]


def test_review_multiple_files_has_no_file_cap_when_limit_is_zero(
    monkeypatch, sample_diff
):
    monkeypatch.setattr(settings, "max_files_per_review", 0)
    client, completions = fake_client(
        model_payload(), model_payload(), "Adds helpers to two files."
    )
    reviewer = CodeReviewer(client=client)

    result = reviewer.review_multiple_files(
        [
            {"filename": "a.py", "diff": sample_diff},
            {"filename": "b.py", "diff": sample_diff},
        ]
    )

    # Two file reviews, then one walkthrough call.
    assert len(completions.calls) == 3
    assert len(result["file_reviews"]) == 2
    assert result["skipped_files"] == []
    assert "exceeded the per-PR review limit" not in result["summary"]


def test_client_property_requires_an_api_key(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_auth_token", "")
    reviewer = CodeReviewer()

    with pytest.raises(ValueError, match="ANTHROPIC_AUTH_TOKEN"):
        _ = reviewer.client


# -- Walkthrough ---------------------------------------------------------------


def test_walkthrough_is_generated_and_rendered(sample_diff):
    client, messages = fake_client(model_payload(), "Adds a division helper with guards.")
    reviewer = CodeReviewer(client=client)

    result = reviewer.review_multiple_files([{"filename": "a.py", "diff": sample_diff}])

    assert result["walkthrough"] == "Adds a division helper with guards."
    # The section sits between the header stats and the breakdown table.
    summary = result["summary"]
    assert "### 🧭 Walkthrough" in summary
    assert summary.index("**Confidence:**") < summary.index("### 🧭 Walkthrough")
    assert summary.index("### 🧭 Walkthrough") < summary.index("### 📊 Breakdown")
    # The walkthrough call is a separate, lighter request.
    walkthrough_call = messages.calls[-1]
    assert "technical writer" in walkthrough_call["system"]
    assert walkthrough_call["max_tokens"] == 400
    assert "a.py: 1 issue(s) flagged" in walkthrough_call["messages"][0]["content"]
    assert "pull request" in walkthrough_call["messages"][0]["content"]


def test_walkthrough_prompt_mentions_incremental_updates(sample_diff):
    client, messages = fake_client(model_payload(), "Small fix.")
    reviewer = CodeReviewer(client=client)

    reviewer.review_multiple_files(
        [{"filename": "a.py", "diff": sample_diff}],
        context={"incremental": True, "pr_title": "Fix divide", "base_sha": "b" * 40},
    )

    assert "incremental update" in messages.calls[-1]["messages"][0]["content"]
    assert "Fix divide" in messages.calls[-1]["messages"][0]["content"]


def test_walkthrough_failure_leaves_the_review_intact(sample_diff, monkeypatch):
    monkeypatch.setattr("reviewbot.llm.reviewer.time.sleep", lambda *_: None)
    client, _ = fake_client(model_payload(), RuntimeError("walkthrough boom"))
    reviewer = CodeReviewer(client=client)

    result = reviewer.review_multiple_files([{"filename": "a.py", "diff": sample_diff}])

    assert result["walkthrough"] is None
    assert "### 🧭 Walkthrough" not in result["summary"]
    assert result["total_bugs"] == 1
    assert "AI Code Review" in result["summary"]


def test_walkthrough_is_skipped_when_there_are_no_files():
    client, messages = fake_client()
    reviewer = CodeReviewer(client=client)

    result = reviewer.review_multiple_files([])

    assert result["walkthrough"] is None
    assert messages.calls == []


def test_incremental_range_is_rendered_in_the_summary(sample_diff):
    client, _ = fake_client(
        model_payload(), "Fixes the helper.", model_payload(), "Fixes the helper."
    )
    reviewer = CodeReviewer(client=client)

    result = reviewer.review_multiple_files(
        [{"filename": "a.py", "diff": sample_diff}],
        context={
            "incremental": True,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
        },
    )

    assert "**Incremental review** of `aaaaaaa...bbbbbbb`" in result["summary"]
    # A full review never shows the incremental note.
    full = reviewer.review_multiple_files([{"filename": "a.py", "diff": sample_diff}])
    assert "Incremental review" not in full["summary"]


def test_explicit_config_overrides_guardrails(sample_diff, monkeypatch):
    from reviewbot.utils.repo_config import EffectiveReviewConfig

    monkeypatch.setattr(settings, "max_files_per_review", 0)
    cfg = EffectiveReviewConfig.from_repo_config(None)
    cfg.max_files_per_review = 1
    client, completions = fake_client(model_payload(), "One file.")
    reviewer = CodeReviewer(client=client, config=cfg)

    result = reviewer.review_multiple_files(
        [
            {"filename": "a.py", "diff": sample_diff},
            {"filename": "b.py", "diff": sample_diff},
        ]
    )

    assert result["skipped_files"] == ["b.py"]


@pytest.mark.skipif(
    os.getenv("REVIEWBOT_LIVE_TESTS") != "1",
    reason="Requires a real ANTHROPIC_AUTH_TOKEN; set REVIEWBOT_LIVE_TESTS=1 to run",
)
def test_review_file_against_the_real_api(sample_diff):
    review = CodeReviewer().review_file("app.py", sample_diff)

    assert isinstance(review["bugs"], list)
    assert 0.0 <= review["confidence"] <= 1.0
