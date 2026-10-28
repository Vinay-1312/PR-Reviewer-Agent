from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

from google.cloud import firestore

# A "processing" claim blocks other deliveries for this long. It must not be shorter than the Cloud Run
# request timeout (300s): after that the original request is certainly dead, so its claim is stale.
LEASE = timedelta(minutes=5)
TERMINAL_STATUSES = {"done", "gave_up"}


@dataclass(frozen=True)
class Claim:
    outcome: Literal["acquired", "busy", "finished"]
    attempts: int = 0


class ReviewStore(Protocol):
    def claim(self, key: str) -> Claim: ...
    def set_status(self, key: str, status: str) -> None: ...


def decide_claim(doc: dict | None, now: datetime) -> Claim:
    """acquired: go ahead. finished: already handled. busy: another delivery holds a live claim."""
    if doc is None:
        return Claim("acquired", 1)
    status = doc.get("status")
    if status in TERMINAL_STATUSES:
        return Claim("finished")
    if status == "processing" and now - doc["updated_at"] < LEASE:
        return Claim("busy")
    return Claim("acquired", doc.get("attempts", 0) + 1)


class FirestoreStore:
    def __init__(self, client: firestore.Client | None = None, collection: str = "reviews") -> None:
        self._db = client or firestore.Client()
        self._collection = self._db.collection(collection)

    def claim(self, key: str) -> Claim:
        ref = self._collection.document(key)

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> Claim:
            now = datetime.now(timezone.utc)
            snapshot = ref.get(transaction=transaction)
            claim = decide_claim(snapshot.to_dict() if snapshot.exists else None, now)
            if claim.outcome == "acquired":
                transaction.set(ref, {"status": "processing", "attempts": claim.attempts, "updated_at": now})
            return claim

        return run(self._db.transaction())

    def set_status(self, key: str, status: str) -> None:
        self._collection.document(key).set(
            {"status": status, "updated_at": datetime.now(timezone.utc)}, merge=True
        )
