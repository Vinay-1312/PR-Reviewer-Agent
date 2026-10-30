from typing import Protocol

import httpx

from pr_reviewer.github.auth import API


class GitHub(Protocol):
    def get_pull(self, repo: str, number: int) -> dict: ...
    def get_diff(self, repo: str, number: int) -> str: ...
    def create_review(self, repo: str, number: int, commit_id: str, body: str, comments: list[dict]) -> None: ...


class GitHubClient:
    def __init__(self, token: str, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(timeout=30)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get_pull(self, repo: str, number: int) -> dict:
        response = self._http.get(f"{API}/repos/{repo}/pulls/{number}", headers=self._headers)
        response.raise_for_status()
        return response.json()

    def get_diff(self, repo: str, number: int) -> str:
        headers = {**self._headers, "Accept": "application/vnd.github.diff"}
        response = self._http.get(f"{API}/repos/{repo}/pulls/{number}", headers=headers)
        response.raise_for_status()
        return response.text

    def create_review(self, repo: str, number: int, commit_id: str, body: str, comments: list[dict]) -> None:
        payload = {"commit_id": commit_id, "body": body, "event": "COMMENT", "comments": comments}
        response = self._http.post(f"{API}/repos/{repo}/pulls/{number}/reviews", headers=self._headers, json=payload)
        response.raise_for_status()
