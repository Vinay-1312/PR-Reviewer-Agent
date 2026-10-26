import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from pr_reviewer.receiver.app import app, get_publisher, get_webhook_secret

SECRET = "test-secret"


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[bytes, dict]] = []
        self.fail = False

    def publish(self, data: bytes, attributes: dict[str, str]) -> None:
        if self.fail:
            raise RuntimeError("pubsub down")
        self.published.append((data, attributes))


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def client(publisher: FakePublisher):
    app.dependency_overrides[get_webhook_secret] = lambda: SECRET
    app.dependency_overrides[get_publisher] = lambda: publisher
    yield TestClient(app)
    app.dependency_overrides.clear()


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pr_payload(**overrides) -> dict:
    payload = {
        "action": "opened",
        "installation": {"id": 99},
        "repository": {"full_name": "octo/repo"},
        "pull_request": {"number": 7, "draft": False, "head": {"sha": "abc123"}},
    }
    payload.update(overrides)
    return payload


def post(client: TestClient, payload: dict, event: str = "pull_request", signature: str | None = None):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": signature if signature is not None else sign(body),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": "delivery-1",
            "Content-Type": "application/json",
        },
    )


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_valid_pr_event_is_queued(client, publisher):
    response = post(client, pr_payload())
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    data, attributes = publisher.published[0]
    assert json.loads(data) == {
        "delivery_id": "delivery-1",
        "action": "opened",
        "installation_id": 99,
        "repo": "octo/repo",
        "pr_number": 7,
        "head_sha": "abc123",
    }
    assert attributes["delivery_id"] == "delivery-1"


def test_bad_signature_is_rejected(client, publisher):
    response = post(client, pr_payload(), signature=sign(b"x", "wrong-secret"))
    assert response.status_code == 401
    assert publisher.published == []


def test_missing_signature_is_rejected(client, publisher):
    body = json.dumps(pr_payload()).encode()
    response = client.post("/webhook", content=body, headers={"X-GitHub-Event": "pull_request"})
    assert response.status_code == 401
    assert publisher.published == []


def test_ping_returns_pong(client, publisher):
    response = post(client, {"zen": "hi"}, event="ping")
    assert response.json() == {"status": "pong"}
    assert publisher.published == []


def test_other_events_are_ignored(client, publisher):
    response = post(client, {"ref": "main"}, event="push")
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert publisher.published == []


@pytest.mark.parametrize("action", ["closed", "labeled", "edited"])
def test_non_reviewable_actions_are_ignored(client, publisher, action):
    response = post(client, pr_payload(action=action))
    assert response.json()["status"] == "ignored"
    assert publisher.published == []


def test_draft_prs_are_ignored(client, publisher):
    payload = pr_payload()
    payload["pull_request"]["draft"] = True
    assert post(client, payload).json()["status"] == "ignored"
    assert publisher.published == []


def test_payload_missing_installation_is_ignored(client, publisher):
    payload = pr_payload()
    del payload["installation"]
    assert post(client, payload).json()["status"] == "ignored"
    assert publisher.published == []


def test_publish_failure_returns_503(client, publisher):
    publisher.fail = True
    assert post(client, pr_payload()).status_code == 503


def test_invalid_json_is_400(client):
    body = b"not json"
    response = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": sign(body), "X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 400


def test_rejection_reasons_are_logged_without_leaking_the_secret(client, caplog):
    body = json.dumps(pr_payload()).encode()
    headers = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1"}

    with caplog.at_level("WARNING", logger="receiver"):
        client.post("/webhook", content=body, headers=headers)
        client.post("/webhook", content=body, headers={**headers, "X-Hub-Signature-256": "abc"})
        client.post("/webhook", content=body, headers={**headers, "X-Hub-Signature-256": sign(body, "wrong")})

    messages = [r.getMessage() for r in caplog.records]
    assert any("missing X-Hub-Signature-256" in m for m in messages)
    assert any("not in sha256=<hex> format" in m for m in messages)
    assert any("signature mismatch" in m and f"{len(SECRET)} chars" in m for m in messages)
    assert all(SECRET not in m and "wrong" not in m for m in messages)


def test_secret_whitespace_is_stripped(monkeypatch):
    from pr_reviewer.receiver.app import get_webhook_secret

    get_webhook_secret.cache_clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "abc123\r\n")
    assert get_webhook_secret() == "abc123"
    get_webhook_secret.cache_clear()
