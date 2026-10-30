import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeReviewer, http_error
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

from pr_reviewer.storage.store import LEASE
from pr_reviewer.worker.app import app, get_services, is_transient
from pr_reviewer.worker.pipeline import Services

MESSAGE = {"installation_id": 1, "repo": "octo/repo", "pr_number": 7, "head_sha": "abc1234"}
KEY = "octo__repo__7__abc1234"


def envelope(message: dict | bytes = MESSAGE) -> dict:
    raw = message if isinstance(message, bytes) else json.dumps(message).encode()
    return {"message": {"data": base64.b64encode(raw).decode(), "messageId": "m1"}, "subscription": "s"}


def use(github, reviewer, store) -> TestClient:
    app.dependency_overrides[get_services] = lambda: Services(
        store=store, reviewer=reviewer, github=lambda installation_id: github
    )
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_healthz(github, reviewer, store):
    assert use(github, reviewer, store).get("/healthz").json() == {"status": "ok"}


def test_successful_review_is_acked_and_recorded(github, reviewer, store):
    response = use(github, reviewer, store).post("/pubsub", json=envelope())
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert len(github.reviews) == 1
    assert store.docs[KEY]["status"] == "done"


def test_redelivery_does_not_review_twice(github, reviewer, store):
    client = use(github, reviewer, store)
    client.post("/pubsub", json=envelope())
    response = client.post("/pubsub", json=envelope())
    assert response.json() == {"status": "duplicate"}
    assert len(github.reviews) == 1


def test_live_claim_by_another_delivery_is_retried_not_acked(github, reviewer, store):
    store.docs[KEY] = {"status": "processing", "attempts": 1, "updated_at": datetime.now(timezone.utc)}
    response = use(github, reviewer, store).post("/pubsub", json=envelope())
    assert response.status_code == 503
    assert github.reviews == [] and reviewer.calls == []
    assert store.docs[KEY]["status"] == "processing"


def test_review_is_recovered_after_a_crashed_worker_lease_expires(github, reviewer, store):
    crashed_at = datetime.now(timezone.utc) - LEASE - timedelta(seconds=10)
    store.docs[KEY] = {"status": "processing", "attempts": 1, "updated_at": crashed_at}
    response = use(github, reviewer, store).post("/pubsub", json=envelope())
    assert response.json()["status"] == "ok"
    assert len(github.reviews) == 1
    assert store.docs[KEY]["attempts"] == 2
    assert store.docs[KEY]["status"] == "done"


def test_malformed_message_is_acked_and_dropped(github, reviewer, store):
    client = use(github, reviewer, store)
    for bad in (b"not json", json.dumps({"repo": "x"}).encode()):
        response = client.post("/pubsub", json=envelope(bad))
        assert response.status_code == 200
        assert response.json()["status"] == "dropped"
    assert github.reviews == []


def test_transient_failure_returns_503_and_can_be_retried(github, store):
    reviewer = FakeReviewer(error=genai_errors.ServerError(503, {"error": {"message": "overloaded"}}))
    client = use(github, reviewer, store)
    assert client.post("/pubsub", json=envelope()).status_code == 503
    assert store.docs[KEY]["status"] == "retry"

    reviewer.error = None
    assert client.post("/pubsub", json=envelope()).json()["status"] == "ok"
    assert store.docs[KEY]["attempts"] == 2


def test_permanent_failure_is_acked_not_retried(github, store):
    reviewer = FakeReviewer(error=ValueError("Gemini returned no text"))
    response = use(github, reviewer, store).post("/pubsub", json=envelope())
    assert response.status_code == 200
    assert response.json() == {"status": "failed"}
    assert store.docs[KEY]["status"] == "gave_up"


@pytest.mark.parametrize(
    "error, expected",
    [
        (http_error(429), True),
        (http_error(502), True),
        (http_error(403, {"retry-after": "30"}), True),
        (http_error(403, {"x-ratelimit-remaining": "0"}), True),
        (http_error(403), False),
        (http_error(404), False),
        (http_error(422), False),
        (genai_errors.ClientError(429, {"error": {"message": "quota"}}), True),
        (genai_errors.ClientError(400, {"error": {"message": "bad request"}}), False),
        (ValueError("bug"), False),
    ],
)
def test_is_transient(error, expected):
    assert is_transient(error) is expected
