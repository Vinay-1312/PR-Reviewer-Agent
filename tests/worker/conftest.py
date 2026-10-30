from datetime import datetime, timezone

import httpx
import pytest

from pr_reviewer.llm.reviewer import Review, ReviewComment
from pr_reviewer.storage.store import decide_claim

DIFF = "\n".join(
    [
        "diff --git a/app.py b/app.py",
        "--- a/app.py",
        "+++ b/app.py",
        "@@ -1,2 +1,3 @@",
        " import os",
        "+import sys",
        " def main():",
    ]
)


def http_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.github.com/x")
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


class FakeGitHub:
    def __init__(self, state="open", head_sha="abc1234", diff=DIFF, review_errors=None):
        self.pull = {"state": state, "head": {"sha": head_sha}, "title": "Add sys", "body": "desc"}
        self.diff = diff
        self.review_errors = list(review_errors or [])
        self.reviews: list[dict] = []

    def get_pull(self, repo, number):
        return self.pull

    def get_diff(self, repo, number):
        return self.diff

    def create_review(self, repo, number, commit_id, body, comments):
        if self.review_errors:
            raise self.review_errors.pop(0)
        self.reviews.append({"commit_id": commit_id, "body": body, "comments": comments})


class FakeReviewer:
    def __init__(self, review=None, error=None):
        self.review_result = review or Review(
            summary="Adds an import.",
            comments=[ReviewComment(path="app.py", line=2, body="Unused import.")],
        )
        self.error = error
        self.calls = []

    def review(self, title, description, annotated_diff):
        self.calls.append((title, description, annotated_diff))
        if self.error:
            raise self.error
        return self.review_result


class MemoryStore:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def claim(self, key):
        now = datetime.now(timezone.utc)
        claim = decide_claim(self.docs.get(key), now)
        if claim.outcome == "acquired":
            self.docs[key] = {"status": "processing", "attempts": claim.attempts, "updated_at": now}
        return claim

    def set_status(self, key, status):
        self.docs[key] = {**self.docs.get(key, {}), "status": status, "updated_at": datetime.now(timezone.utc)}


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def reviewer():
    return FakeReviewer()


@pytest.fixture
def store():
    return MemoryStore()
