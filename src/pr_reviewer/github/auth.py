import time

import httpx
import jwt

API = "https://api.github.com"
JWT_LIFETIME_SECONDS = 9 * 60  # GitHub allows at most 10 minutes


def create_app_jwt(app_id: str, private_key: str, now: float | None = None) -> str:
    issued = int(time.time() if now is None else now)
    claims = {"iat": issued - 60, "exp": issued + JWT_LIFETIME_SECONDS, "iss": app_id}
    return jwt.encode(claims, private_key, algorithm="RS256")


def get_installation_token(
    app_id: str,
    private_key: str,
    installation_id: int,
    http: httpx.Client | None = None,
) -> str:
    """Exchange a signed app JWT for a one-hour token scoped to this installation's repos."""
    client = http or httpx.Client(timeout=30)
    response = client.post(
        f"{API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {create_app_jwt(app_id, private_key)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    response.raise_for_status()
    return response.json()["token"]
