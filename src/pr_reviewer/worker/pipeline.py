"""The review itself: fetch the PR, ask the model, post the result."""

import logging
from dataclasses import dataclass
from typing import Callable

import httpx

from pr_reviewer.common.messages import ReviewRequest
from pr_reviewer.github.client import GitHub
from pr_reviewer.github.diff import annotate_diff, truncate
from pr_reviewer.llm.reviewer import Review, ReviewComment, Reviewer
from pr_reviewer.llm.sanitize import sanitize_path, sanitize_review
from pr_reviewer.storage.store import ReviewStore

log = logging.getLogger("worker")

MAX_DIFF_CHARS = 100_000
MAX_COMMENTS = 20
FOOTER = "_Automated review. Treat it as a second opinion, not a verdict._"


@dataclass
class Services:
    store: ReviewStore
    reviewer: Reviewer
    github: Callable[[int], GitHub]  # installation id -> client


def split_comments(
    comments: list[ReviewComment], commentable: dict[str, set[int]]
) -> tuple[list[ReviewComment], list[ReviewComment]]:
    """Separate comments GitHub will accept inline from ones that point at lines outside the diff."""
    inline: list[ReviewComment] = []
    leftover: list[ReviewComment] = []
    for comment in comments[:MAX_COMMENTS]:
        if comment.line in commentable.get(comment.path, set()):
            inline.append(comment)
        else:
            leftover.append(comment)
    return inline, leftover


def format_body(summary: str, leftover: list[ReviewComment]) -> str:
    parts = [summary.strip()]
    if leftover:
        notes = "\n".join(f"- `{sanitize_path(c.path)}:{c.line}`: {c.body.strip()}" for c in leftover)
        parts.append(f"**Other notes**\n{notes}")
    parts.append(FOOTER)
    return "\n\n".join(parts)


def process(request: ReviewRequest, services: Services) -> str:
    """Review one PR commit. Returns a short outcome label for logging."""
    github = services.github(request.installation_id)

    pull = github.get_pull(request.repo, request.pr_number)
    if pull.get("state") != "open":
        return "skipped: pull request is not open"
    if pull["head"]["sha"] != request.head_sha:
        return "skipped: a newer commit exists"

    diff = github.get_diff(request.repo, request.pr_number)
    if not diff.strip():
        return "skipped: empty diff"

    annotated, commentable = annotate_diff(diff)
    review: Review = sanitize_review(
        services.reviewer.review(pull.get("title", ""), pull.get("body") or "", truncate(annotated, MAX_DIFF_CHARS))
    )

    inline, leftover = split_comments(review.comments, commentable)
    payload = [{"path": c.path, "line": c.line, "side": "RIGHT", "body": c.body} for c in inline]
    try:
        github.create_review(
            request.repo, request.pr_number, request.head_sha, format_body(review.summary, leftover), payload
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 422 or not inline:
            raise
        # GitHub rejected an inline position; fall back to one review with everything in the body.
        log.warning("inline comments rejected for %s#%s, posting as plain review", request.repo, request.pr_number)
        github.create_review(
            request.repo, request.pr_number, request.head_sha, format_body(review.summary, inline + leftover), []
        )
    return f"reviewed: {len(inline)} inline, {len(leftover)} in body"
