"""Tests for scripts/check_codex_review.py.

Fake REST/GraphQL payloads shaped after real GitHub responses: the review body
Codex posts when it has findings, the :+1: it leaves when it does not, and the
thread resolution state the protocol turns on.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_codex_review as check  # noqa: E402

_GATE_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "codex-review-gate.yml"
)

REPO = "Data-Policy-and-Innovation-Centre/ai-for-panchayats"
HEAD = "f319c879a1b2c3d4e5f60718293a4b5c6d7e8f90"
HEAD_TIME = "2026-08-08T12:00:00Z"
CODEX = {"login": "chatgpt-codex-connector[bot]"}
HUMAN = {"login": "ymohanty"}
CODE_FILE = "src/database/build.py"

# Abridged from a real body; the parser only needs the commit line.
CODEX_REVIEW_BODY = """
### 💡 Codex Review

Here are some automated review suggestions for this pull request.

**Reviewed commit:** `{sha}`

<details> <summary>ℹ️ About Codex in GitHub</summary>
If Codex has suggestions, it will comment; otherwise it will react with 👍.
</details>
"""

FINDING_BODY = (
    "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)"
    "</sub></sub>  Drop the DuckDB tables in dependency order**\n\n"
    "The reset drops parents before children, so the rebuild fails."
)


class FakeGitHub:
    """Minimal stand-in for the REST + GraphQL surfaces the gate reads."""

    def __init__(
        self,
        *,
        head_sha: str = HEAD,
        head_time: str = HEAD_TIME,
        draft: bool = False,
        labels: list[str] | None = None,
        files: list[str] | None = None,
        changed_lines: int = 20,
        reviews: list[dict[str, Any]] | None = None,
        comments: list[dict[str, Any]] | None = None,
        reactions: dict[str, list[dict[str, Any]]] | None = None,
        threads: list[dict[str, Any]] | None = None,
    ) -> None:
        self.head_sha = head_sha
        self.head_time = head_time
        self.draft = draft
        self.labels = labels or []
        self.files = files if files is not None else [CODE_FILE]
        self.changed_lines = changed_lines
        self.reviews = reviews or []
        self.comments = comments or []
        self.reactions = reactions or {}
        self.threads = threads or []

    def rest(self, path: str) -> Any:
        if path.startswith("pulls/") and path.endswith("/files"):
            per_file, extra = divmod(self.changed_lines, len(self.files))
            return [
                {"filename": name, "additions": per_file + (extra if i == 0 else 0)}
                for i, name in enumerate(self.files)
            ]
        if path.startswith("pulls/") and path.endswith("/reviews"):
            return self.reviews
        if path.startswith("pulls/"):
            return {
                "head": {"sha": self.head_sha},
                "draft": self.draft,
                "labels": [{"name": name} for name in self.labels],
            }
        if path.startswith("commits/"):
            return {"commit": {"committer": {"date": self.head_time}}}
        if path.endswith("/comments"):
            return self.comments
        if path.endswith("/reactions"):
            return self.reactions.get(path, [])
        raise AssertionError(f"unexpected REST path: {path}")

    def graphql(self, query: str, variables: dict[str, Any]) -> Any:
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": self.threads,
                        }
                    }
                }
            }
        }

    def evaluate(self):
        return check.evaluate(self.rest, self.graphql, REPO, 42)


def codex_review(sha: str) -> dict[str, Any]:
    return {"user": CODEX, "body": CODEX_REVIEW_BODY.format(sha=sha), "state": "COMMENTED"}


def thread(*, resolved: bool, author: dict[str, str] = CODEX, body: str = FINDING_BODY):
    return {
        "isResolved": resolved,
        "comments": {
            "nodes": [
                {
                    "author": {"login": author["login"].removesuffix("[bot]")},
                    "body": body,
                    "path": CODE_FILE,
                }
            ]
        },
    }


def thumbs_up(at: str, *, user: dict[str, str] = CODEX) -> dict[str, Any]:
    return {"content": "+1", "user": user, "created_at": at}


# --------------------------------------------------------------------------
# The clean path: Codex reacts and posts nothing
# --------------------------------------------------------------------------


def test_thumbs_up_after_head_commit_passes():
    api = FakeGitHub(
        comments=[{"id": 1, "reactions": {"total_count": 1}}],
        reactions={"issues/comments/1/reactions": [thumbs_up("2026-08-08T12:05:00Z")]},
    )
    verdict = api.evaluate()
    assert verdict.ok
    assert "cleared" in verdict.summary


def test_thumbs_up_on_pull_request_body_passes():
    api = FakeGitHub(reactions={"issues/42/reactions": [thumbs_up("2026-08-08T12:05:00Z")]})
    assert api.evaluate().ok


def test_thumbs_up_predating_head_commit_fails():
    """A push after the clean run invalidates it; the reaction carries no sha."""
    api = FakeGitHub(
        comments=[{"id": 1, "reactions": {"total_count": 1}}],
        reactions={"issues/comments/1/reactions": [thumbs_up("2026-08-08T11:00:00Z")]},
    )
    verdict = api.evaluate()
    assert not verdict.ok
    assert "predates the head commit" in verdict.summary


def test_thumbs_up_from_a_human_is_not_the_signal():
    api = FakeGitHub(
        comments=[{"id": 1, "reactions": {"total_count": 1}}],
        reactions={
            "issues/comments/1/reactions": [thumbs_up("2026-08-08T12:05:00Z", user=HUMAN)]
        },
    )
    assert not api.evaluate().ok


def test_comments_without_reactions_are_not_fetched():
    """total_count == 0 must not cost a request; the sweep runs every 5 minutes."""
    api = FakeGitHub(comments=[{"id": 9, "reactions": {"total_count": 0}}])
    api.reactions = {}  # a fetch for comment 9 would raise in rest()
    assert not api.evaluate().ok


# --------------------------------------------------------------------------
# The findings path: Codex reviews, threads must be answered
# --------------------------------------------------------------------------


def test_review_of_head_with_all_threads_resolved_passes():
    api = FakeGitHub(
        reviews=[codex_review(HEAD[:10])],
        threads=[thread(resolved=True), thread(resolved=True)],
    )
    verdict = api.evaluate()
    assert verdict.ok
    assert "all answered" in verdict.summary


def test_unresolved_codex_thread_fails_and_names_the_finding():
    api = FakeGitHub(reviews=[codex_review(HEAD[:10])], threads=[thread(resolved=False)])
    verdict = api.evaluate()
    assert not verdict.ok
    assert "1 Codex finding(s) unanswered" in verdict.title
    assert "Drop the DuckDB tables in dependency order" in verdict.summary
    assert CODE_FILE in verdict.summary


def test_unresolved_human_thread_does_not_fail_the_gate():
    """Reviewer notes on our own PRs stay open by habit; the gate is about Codex."""
    api = FakeGitHub(
        reviews=[codex_review(HEAD[:10])],
        threads=[thread(resolved=False, author=HUMAN, body="Schema matches the manifest.")],
    )
    assert api.evaluate().ok


def test_review_of_an_older_commit_fails():
    api = FakeGitHub(reviews=[codex_review("9869703727")])
    verdict = api.evaluate()
    assert not verdict.ok
    assert "9869703727" in verdict.summary
    assert HEAD[:10] in verdict.title


def test_no_codex_activity_at_all_fails():
    verdict = FakeGitHub().evaluate()
    assert not verdict.ok
    assert "@codex review" in verdict.summary
    assert "no review or reaction" in verdict.summary


def test_unresolved_thread_fails_even_when_a_fresh_thumbs_up_exists():
    """Do not merge with review comments left unanswered."""
    api = FakeGitHub(
        comments=[{"id": 1, "reactions": {"total_count": 1}}],
        reactions={"issues/comments/1/reactions": [thumbs_up("2026-08-08T12:05:00Z")]},
        threads=[thread(resolved=False)],
    )
    assert not api.evaluate().ok


# --------------------------------------------------------------------------
# Codex out of review credits
# --------------------------------------------------------------------------

# Verbatim from a real refusal, where the account hit its limit mid-round.
QUOTA_BODY = (
    "You have reached your Codex usage limits for code reviews. You can see "
    "your limits in the [Codex usage dashboard](https://chatgpt.com/codex/"
    "cloud/settings/usage).\nTo continue using code reviews, you can upgrade "
    "your account or add credits to your account."
)


def quota_comment(at: str) -> dict[str, Any]:
    return {"id": 7, "user": CODEX, "body": QUOTA_BODY, "created_at": at}


def test_quota_refusal_is_reported_as_itself_not_as_a_missing_review():
    api = FakeGitHub(comments=[quota_comment("2026-08-08T19:03:54Z")])
    verdict = api.evaluate()
    assert not verdict.ok
    assert verdict.title == "Codex is out of review credits"
    assert "`@codex review` will not help" in verdict.summary
    assert "codex-review-not-required" in verdict.summary


def test_quota_refusal_still_fails_the_gate():
    """Passing here would switch the gate off exactly when it cannot do its job."""
    api = FakeGitHub(comments=[quota_comment("2026-08-08T19:03:54Z")])
    assert api.evaluate().conclusion == "failure"


def test_quota_refusal_predating_the_head_does_not_change_the_message():
    """Credits may have been topped up since; the ordinary advice applies again."""
    api = FakeGitHub(comments=[quota_comment("2026-08-08T11:00:00Z")])
    verdict = api.evaluate()
    assert not verdict.ok
    assert verdict.title.startswith("Codex has not reviewed")


def test_a_review_of_the_head_beats_an_earlier_quota_refusal():
    api = FakeGitHub(
        comments=[quota_comment("2026-08-08T12:05:00Z")],
        reviews=[codex_review(HEAD[:10])],
        threads=[thread(resolved=True)],
    )
    assert api.evaluate().ok


def test_quota_wording_from_a_human_is_not_the_signal():
    api = FakeGitHub(
        comments=[
            {
                "id": 7,
                "user": HUMAN,
                "body": "we have reached your Codex usage limits for code reviews",
                "created_at": "2026-08-08T19:03:54Z",
            }
        ]
    )
    assert api.evaluate().title.startswith("Codex has not reviewed")


# --------------------------------------------------------------------------
# Exemptions
# --------------------------------------------------------------------------


def test_small_docs_only_branch_is_exempt():
    api = FakeGitHub(files=["docs/DATABASE.md", "AGENTS.md"], changed_lines=30)
    verdict = api.evaluate()
    assert verdict.ok
    assert "Docs-only" in verdict.summary


def test_large_docs_only_branch_is_not_exempt():
    """Only *small* docs branches are exempt; a policy rewrite is not one."""
    api = FakeGitHub(
        files=["docs/DATABASE.md"], changed_lines=check.DOCS_ONLY_MAX_LINES + 1
    )
    assert not api.evaluate().ok


def test_docs_exemption_holds_exactly_at_the_threshold():
    api = FakeGitHub(files=["docs/DATABASE.md"], changed_lines=check.DOCS_ONLY_MAX_LINES)
    assert api.evaluate().ok


def test_large_docs_only_branch_can_still_use_the_label():
    api = FakeGitHub(
        files=["docs/DATABASE.md"],
        changed_lines=check.DOCS_ONLY_MAX_LINES + 1,
        labels=["codex-review-not-required"],
    )
    assert api.evaluate().ok


def test_one_code_file_removes_the_docs_exemption():
    api = FakeGitHub(files=["docs/DATABASE.md", CODE_FILE])
    assert not api.evaluate().ok


def test_empty_diff_is_not_treated_as_docs_only():
    assert check.is_docs_only([]) is False


def test_changed_lines_counts_both_sides_of_the_diff():
    files = [{"additions": 10, "deletions": 5}, {"additions": 1, "deletions": 0}]
    assert check.changed_lines(files) == 16


def test_renaming_code_into_docs_is_not_docs_only():
    """A pure rename adds no lines, so the size cap would not catch it either."""
    files = [
        {
            "filename": "docs/build.md",
            "previous_filename": CODE_FILE,
            "status": "renamed",
            "additions": 0,
            "deletions": 0,
        }
    ]
    assert check.is_docs_only(files) is False


def test_renaming_one_doc_to_another_stays_docs_only():
    files = [
        {
            "filename": "docs/DATABASE.md",
            "previous_filename": "DATABASE.md",
            "status": "renamed",
            "additions": 0,
            "deletions": 0,
        }
    ]
    assert check.is_docs_only(files) is True


# --------------------------------------------------------------------------
# The verdict is bound to the sha it was computed against
# --------------------------------------------------------------------------


def test_verdict_carries_the_evaluated_head():
    api = FakeGitHub(reviews=[codex_review(HEAD[:10])], threads=[thread(resolved=True)])
    assert api.evaluate().head_sha == HEAD


def test_check_run_is_posted_to_the_evaluated_head_not_a_refetched_one():
    """A push landing mid-run must not inherit the previous head's pass."""
    old, new = "a" * 40, "b" * 40
    api = FakeGitHub(head_sha=old, reviews=[codex_review(old[:10])], threads=[])
    verdict = api.evaluate()
    assert verdict.ok

    api.head_sha = new  # the branch moves while the gate is still running
    posted: dict[str, Any] = {}

    def fake_request(url, token, method="GET", body=None):
        posted.update(body)
        return {}

    original = check._request
    check._request = fake_request
    try:
        check.post_check_run(REPO, "token", "codex-review", verdict)
    finally:
        check._request = original

    assert posted["head_sha"] == old
    assert posted["conclusion"] == "success"


def test_skip_label_is_exempt():
    api = FakeGitHub(labels=["codex-review-not-required"])
    verdict = api.evaluate()
    assert verdict.ok
    assert "codex-review-not-required" in verdict.summary


def test_draft_pull_requests_pass():
    assert FakeGitHub(draft=True).evaluate().ok


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "login, expected",
    [
        ("chatgpt-codex-connector[bot]", True),  # REST spelling
        ("chatgpt-codex-connector", True),  # GraphQL spelling
        ("github-actions[bot]", False),
        ("ymohanty", False),
        (None, False),
    ],
)
def test_codex_login_is_recognised_in_both_api_spellings(login, expected):
    assert check.is_codex(login) is expected


def test_reviewed_shas_ignores_non_codex_reviews():
    reviews = [
        {"user": HUMAN, "body": "**Reviewed commit:** `deadbeef12`"},
        codex_review("f319c879a1"),
    ]
    assert check.reviewed_shas(reviews) == ["f319c879a1"]


def test_reviewed_shas_accepts_the_current_structured_commit_without_legacy_body_text():
    reviews = [
        {
            "user": CODEX,
            "body": "### Codex Review\n\nOne finding follows.",
            "commit_id": HEAD,
            "state": "COMMENTED",
        }
    ]
    assert check.reviewed_shas(reviews) == [HEAD]


def test_reviewed_shas_prefers_structured_commit_over_legacy_body_text():
    reviews = [
        {
            "user": CODEX,
            "body": CODEX_REVIEW_BODY.format(sha="deadbeef12"),
            "commit_id": HEAD,
            "state": "COMMENTED",
        }
    ]
    assert check.reviewed_shas(reviews) == [HEAD]


def test_reviewed_shas_rejects_an_abbreviated_structured_commit_id():
    reviews = [
        {
            "user": CODEX,
            "body": CODEX_REVIEW_BODY.format(sha=HEAD[:10]),
            "commit_id": "deadbee",
            "state": "COMMENTED",
        }
    ]
    assert check.reviewed_shas(reviews) == [HEAD[:10]]


def test_reviewed_shas_keeps_legacy_body_fallback():
    assert check.reviewed_shas([codex_review("f319c879a1")]) == ["f319c879a1"]


def test_reviewed_shas_keeps_posting_order():
    reviews = [codex_review("aaaaaaaaaa"), codex_review("bbbbbbbbbb")]
    assert check.reviewed_shas(reviews) == ["aaaaaaaaaa", "bbbbbbbbbb"]


def test_thread_label_strips_the_priority_badge():
    label = check._thread_label({"path": "a/b.py", "body": FINDING_BODY})
    assert label == "a/b.py: Drop the DuckDB tables in dependency order"


def test_paginated_review_threads_are_all_inspected():
    pages = [
        {
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
            "nodes": [thread(resolved=True)],
        },
        {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [thread(resolved=False)],
        },
    ]
    seen: list[str | None] = []

    def graphql(query: str, variables: dict[str, Any]) -> Any:
        seen.append(variables["cursor"])
        page = pages[len(seen) - 1]
        return {"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}

    unresolved = check.unresolved_codex_threads(graphql, "owner", "name", 42)
    assert seen == [None, "cursor1"]
    assert len(unresolved) == 1


def test_gate_workflow_queues_pending_runs_instead_of_cancelling_them():
    """The gate fires on five overlapping per-PR triggers sharing one
    concurrency group (push, review, review comment, issue comment, cron).
    `cancel-in-progress: false` alone is not enough: GitHub still cancels a
    *pending* run the instant another event queues behind it in the same
    group -- that is the documented default, independent of
    cancel-in-progress -- so a push followed quickly by `@codex review`
    followed by a review submission still produces CANCELLED `evaluate` jobs,
    which is what this gate exists to avoid. `queue: max` is the only way to
    let more than one run wait in a group, and GitHub rejects it paired with
    `cancel-in-progress: true`, so both settings must hold together."""
    workflow = yaml.safe_load(_GATE_WORKFLOW_PATH.read_text())
    concurrency = workflow["concurrency"]

    assert concurrency["cancel-in-progress"] is False
    assert concurrency["queue"] == "max"
    # Per-PR, not per-workflow-run or repo-wide: two different PRs must not
    # queue behind each other.
    assert "github.event.pull_request.number" in concurrency["group"]
