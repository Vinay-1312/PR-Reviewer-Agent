from pydantic import BaseModel


class ReviewRequest(BaseModel):
    """The message the receiver publishes to Pub/Sub for each reviewable PR event."""

    delivery_id: str = ""
    action: str = ""
    installation_id: int
    repo: str
    pr_number: int
    head_sha: str

    @property
    def key(self) -> str:
        """One review per commit: a new push gets a fresh key, a redelivered event does not."""
        return f"{self.repo.replace('/', '__')}__{self.pr_number}__{self.head_sha}"
