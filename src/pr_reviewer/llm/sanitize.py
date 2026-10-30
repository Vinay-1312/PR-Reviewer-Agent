"""Neutralise model output before it is posted to GitHub.

A prompt-injected model could try to ping people, embed tracking images, smuggle HTML, or link to
phishing pages. Everything outside code spans is defanged here; code spans are left alone because
GitHub renders them as literal text.
"""

import re

from pr_reviewer.llm.reviewer import Review, ReviewComment

MAX_COMMENT_CHARS = 1500
MAX_SUMMARY_CHARS = 2000
MAX_PATH_CHARS = 200

CODE_SPAN = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
URL = re.compile(r"https?://[^\s<>()\[\]`]+")
MENTION = re.compile(r"(?<![\w`@])@([A-Za-z0-9][A-Za-z0-9_/-]*)")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f​-\u200F\u202A-\u202E⁠﻿]")


def _defang(text: str) -> str:
    text = IMAGE.sub("[image removed]", text)
    text = LINK.sub(r"\1", text)
    text = URL.sub(lambda m: f"`{m.group(0)}`", text)
    text = MENTION.sub(lambda m: f"`@{m.group(1)}`", text)
    return text.replace("<", "&lt;")


def sanitize_text(text: str, limit: int) -> str:
    text = CONTROL_CHARS.sub("", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
        # a cut can leave a code span open, which would swallow the rest of the comment
        if text.count("`") % 2:
            text += "`"
    parts = CODE_SPAN.split(text)
    # re.split with a capture group puts code spans at odd indexes
    return "".join(part if i % 2 else _defang(part) for i, part in enumerate(parts))


def sanitize_path(path: str) -> str:
    return re.sub(r"[`\r\n<>]", "", CONTROL_CHARS.sub("", path))[:MAX_PATH_CHARS]


def sanitize_review(review: Review) -> Review:
    return Review(
        summary=sanitize_text(review.summary, MAX_SUMMARY_CHARS),
        comments=[
            ReviewComment(
                path=comment.path,  # matched against the real diff before use, so left untouched here
                line=comment.line,
                body=sanitize_text(comment.body, MAX_COMMENT_CHARS),
            )
            for comment in review.comments
        ],
    )
