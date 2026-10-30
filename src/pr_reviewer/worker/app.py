"""Private Pub/Sub push endpoint that reviews pull requests.

Return codes matter here: 2xx acknowledges the message, anything else makes Pub/Sub redeliver it.
Only genuinely transient failures return 503; everything else is acknowledged and logged.
Pub/Sub caps redeliveries (max_delivery_attempts) and then moves the message to the dead-letter topic.
"""

import base64
import logging
import os
from functools import lru_cache

import httpx
from fastapi import Depends, FastAPI, HTTPException
from google.genai import errors as genai_errors
from pydantic import BaseModel

from pr_reviewer.common.messages import ReviewRequest
from pr_reviewer.github.auth import get_installation_token
from pr_reviewer.github.client import GitHubClient
from pr_reviewer.llm.reviewer import GeminiReviewer
from pr_reviewer.storage.store import FirestoreStore
from pr_reviewer.worker.pipeline import Services, process

log = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO)

DEFAULT_MODEL = "gemini-3.8-flash"

app = FastAPI(title="PR review worker")


class PubSubMessage(BaseModel):
    data: str = ""
    messageId: str = ""


class PubSubEnvelope(BaseModel):
    message: PubSubMessage
    subscription: str = ""


@lru_cache
def get_services() -> Services:
    app_id = os.environ["GITHUB_APP_ID"].strip()
    private_key = os.environ["GITHUB_APP_PRIVATE_KEY"].strip()
    return Services(
        store=FirestoreStore(),
        reviewer=GeminiReviewer(
            os.environ["GEMINI_API_KEY"].strip(), os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        ),
        github=lambda installation_id: GitHubClient(get_installation_token(app_id, private_key, installation_id)),
    )


def is_transient(error: Exception) -> bool:
    """True if retrying later could plausibly succeed (rate limits, outages, network)."""
    if isinstance(error, httpx.TransportError):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        rate_limited = response.status_code == 403 and (
            "retry-after" in response.headers or response.headers.get("x-ratelimit-remaining") == "0"
        )
        return response.status_code == 429 or response.status_code >= 500 or rate_limited
    if isinstance(error, genai_errors.APIError):
        return error.code == 429 or (error.code or 0) >= 500
    return False


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/pubsub")
def pubsub(envelope: PubSubEnvelope, services: Services = Depends(get_services)) -> dict:
    try:
        request = ReviewRequest.model_validate_json(base64.b64decode(envelope.message.data))
    except Exception:
        log.exception("dropping malformed message %s", envelope.message.messageId)
        return {"status": "dropped", "reason": "malformed message"}

    label = f"{request.repo}#{request.pr_number}@{request.head_sha[:7]}"
    claim = services.store.claim(request.key)
    if claim.outcome == "finished":
        log.info("skipping %s: already handled", label)
        return {"status": "duplicate"}
    if claim.outcome == "busy":
        # Don't ack: if the other delivery crashed, a later retry reclaims the review once its lease expires.
        log.info("%s is being processed by another delivery; asking Pub/Sub to retry", label)
        raise HTTPException(status_code=503, detail="review in progress, retry")
    attempts = claim.attempts

    try:
        outcome = process(request, services)
    except Exception as error:
        if is_transient(error):
            log.warning("transient failure on %s (attempt %d): %r", label, attempts, error)
            services.store.set_status(request.key, "retry")
            raise HTTPException(status_code=503, detail="transient failure, retry")
        log.exception("permanent failure on %s", label)
        services.store.set_status(request.key, "gave_up")
        return {"status": "failed"}

    services.store.set_status(request.key, "done")
    log.info("%s: %s", label, outcome)
    return {"status": "ok", "outcome": outcome}
