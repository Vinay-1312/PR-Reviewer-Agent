"""Public GitHub webhook receiver.

Verifies the webhook signature, filters to pull request events worth reviewing,
and publishes a small message to Pub/Sub for the private worker to process.
"""

import hashlib
import hmac
import json
import logging
import os
from functools import lru_cache
from typing import Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Request

log = logging.getLogger("receiver")
logging.basicConfig(level=logging.INFO)

REVIEWABLE_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
PUBLISH_TIMEOUT_SECONDS = 10

app = FastAPI(title="PR webhook receiver")


class Publisher(Protocol):
    def publish(self, data: bytes, attributes: dict[str, str]) -> None: ...


class PubSubPublisher:
    def __init__(self, project_id: str, topic: str) -> None:
        from google.cloud import pubsub_v1

        self._client = pubsub_v1.PublisherClient()
        self._topic_path = self._client.topic_path(project_id, topic)

    def publish(self, data: bytes, attributes: dict[str, str]) -> None:
        future = self._client.publish(self._topic_path, data, **attributes)
        future.result(timeout=PUBLISH_TIMEOUT_SECONDS)


@lru_cache
def get_publisher() -> Publisher:
    return PubSubPublisher(os.environ["PROJECT_ID"], os.environ["TOPIC"])


@lru_cache
def get_webhook_secret() -> str:
    raw = os.environ["GITHUB_WEBHOOK_SECRET"]
    secret = raw.strip()
    if secret != raw:
        # e.g. a trailing newline added when the secret version was created from a shell pipe
        log.warning("webhook secret had surrounding whitespace; stripped %d chars", len(raw) - len(secret))
    return secret


def signature_failure(secret: str, body: bytes, header: str | None) -> str | None:
    """Return why the signature is invalid, or None if it is valid. Never includes secret material."""
    if not header:
        return "missing X-Hub-Signature-256 header"
    if not header.startswith("sha256="):
        return "X-Hub-Signature-256 header is not in sha256=<hex> format"
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        return f"signature mismatch (body {len(body)} bytes, configured secret {len(secret)} chars)"
    return None


def build_message(payload: dict, delivery_id: str) -> dict | None:
    """Reduce the webhook payload to what the worker needs; None if it should be skipped."""
    if payload.get("action") not in REVIEWABLE_ACTIONS:
        return None
    pr = payload.get("pull_request") or {}
    if pr.get("draft"):
        return None
    installation = payload.get("installation") or {}
    repo = payload.get("repository") or {}
    if not (installation.get("id") and repo.get("full_name") and pr.get("number")):
        return None
    return {
        "delivery_id": delivery_id,
        "action": payload["action"],
        "installation_id": installation["id"],
        "repo": repo["full_name"],
        "pr_number": pr["number"],
        "head_sha": (pr.get("head") or {}).get("sha"),
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str = Header(default=""),
    secret: str = Depends(get_webhook_secret),
    publisher: Publisher = Depends(get_publisher),
) -> dict:
    body = await request.body()
    failure = signature_failure(secret, body, x_hub_signature_256)
    if failure:
        log.warning("rejected delivery %s (event %s): %s", x_github_delivery, x_github_event, failure)
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event == "ping":
        return {"status": "pong"}
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"event {x_github_event}"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")

    message = build_message(payload, x_github_delivery)
    if message is None:
        return {"status": "ignored", "reason": "not a reviewable pull request event"}

    try:
        publisher.publish(
            json.dumps(message).encode(),
            {"delivery_id": x_github_delivery, "repo": message["repo"]},
        )
    except Exception:
        log.exception("publish failed for delivery %s", x_github_delivery)
        raise HTTPException(status_code=503, detail="could not enqueue event")

    log.info("queued %s#%s (%s)", message["repo"], message["pr_number"], x_github_delivery)
    return {"status": "queued"}
