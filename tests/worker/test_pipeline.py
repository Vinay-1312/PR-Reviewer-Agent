from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeGitHub, FakeReviewer, http_error

from pr_reviewer.common.messages import ReviewRequest
from pr_reviewer.llm.reviewer import Review, ReviewComment
from pr_reviewer.storage.store import LEASE, Claim, decide_claim
from pr_reviewer.worker.pipeline import Services, process

REQUEST = ReviewRequest(installation_id=1, repo="octo/repo", pr_number=7, head_sha="abc1234")


def services(github, reviewer, store):
    return Services(store=store, reviewer=reviewer, github=lambda installation_id: github)


def test_valid_comment_is_posted_inline(github, reviewer, store):
    outcome = process(REQUEST, services(github, reviewer, store))
    assert outcome == "reviewed: 1 inline, 0 in body"
    review = github.reviews[0]
    assert review["commit_id"] == "abc1234"
    assert review["comments"] == [{"path": "app.py", "line": 2, "side": "RIGHT", "body": "Unused import."}]
    assert "Adds an import." in review["body"]


def test_model_sees_line_numbers(github, reviewer, store):
    process(REQUEST, services(github, reviewer, store))
    assert "    2 +import sys" in reviewer.calls[0][2]


def test_comments_outside_the_diff_move_into_the_body(github, store):
    reviewer = FakeReviewer(
        Review(
            summary="Summary.",
            comments=[
                ReviewComment(path="app.py", line=999, body="Off the diff."),
                ReviewComment(path="other.py", line=1, body="Unknown file."),
            ],
        )
    )
    outcome = process(REQUEST, services(github, reviewer, store))
    assert outcome == "reviewed: 0 inline, 2 in body"
    review = github.reviews[0]
    assert review["comments"] == []
    assert "`app.py:999`: Off the diff." in review["body"]
    assert "`other.py:1`: Unknown file." in review["body"]


def test_github_422_falls_back_to_a_plain_review(reviewer, store):
    github = FakeGitHub(review_errors=[http_error(422)])
    process(REQUEST, services(github, reviewer, store))
    assert len(github.reviews) == 1
    assert github.reviews[0]["comments"] == []
    assert "Unused import." in github.reviews[0]["body"]


def test_non_422_github_errors_propagate(reviewer, store):
    github = FakeGitHub(review_errors=[http_error(500)])
    with pytest.raises(Exception):
        process(REQUEST, services(github, reviewer, store))


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"state": "closed"}, "not open"),
        ({"head_sha": "newer999"}, "newer commit"),
        ({"diff": "  \n"}, "empty diff"),
    ],
)
def test_skip_conditions(reviewer, store, kwargs, expected):
    github = FakeGitHub(**kwargs)
    assert expected in process(REQUEST, services(github, reviewer, store))
    assert github.reviews == [] and reviewer.calls == []


def test_decide_claim():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert decide_claim(None, now) == Claim("acquired", 1)
    assert decide_claim({"status": "done", "attempts": 1, "updated_at": now}, now) == Claim("finished")
    assert decide_claim({"status": "gave_up", "attempts": 5, "updated_at": now}, now) == Claim("finished")
    assert decide_claim({"status": "processing", "attempts": 1, "updated_at": now}, now) == Claim("busy")
    just_inside = now - LEASE + timedelta(seconds=1)
    assert decide_claim({"status": "processing", "attempts": 1, "updated_at": just_inside}, now) == Claim("busy")
    stale = now - LEASE - timedelta(seconds=1)
    assert decide_claim({"status": "processing", "attempts": 1, "updated_at": stale}, now) == Claim("acquired", 2)
    assert decide_claim({"status": "retry", "attempts": 2, "updated_at": now}, now) == Claim("acquired", 3)


def test_model_output_is_sanitised_before_posting(github, store):
    reviewer = FakeReviewer(
        Review(
            summary="Fine. ![x](https://evil.example/t.png) cc @victim",
            comments=[ReviewComment(path="app.py", line=2, body="See https://evil.example/login")],
        )
    )
    process(REQUEST, services(github, reviewer, store))
    review = github.reviews[0]
    assert "evil.example/t.png" not in review["body"]
    assert "`@victim`" in review["body"]
    assert review["comments"][0]["body"] == "See `https://evil.example/login`"


def test_leftover_note_path_cannot_break_out_of_its_code_span(github, store):
    reviewer = FakeReviewer(
        Review(summary="s", comments=[ReviewComment(path="x`\n@victim", line=1, body="b")])
    )
    process(REQUEST, services(github, reviewer, store))
    assert "`x@victim:1`" in github.reviews[0]["body"]
