import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pr_reviewer.github.auth import create_app_jwt, get_installation_token


def make_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


def test_app_jwt_is_signed_with_the_private_key_and_short_lived():
    private, public = make_keypair()
    token = create_app_jwt("12345", private, now=1_000_000)
    claims = jwt.decode(token, public, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iss": "12345", "iat": 1_000_000 - 60, "exp": 1_000_000 + 540}


def test_installation_token_exchange():
    private, public = make_keypair()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(201, json={"token": "ghs_installation"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_installation_token("12345", private, 77, http=http) == "ghs_installation"
    assert seen["url"] == "https://api.github.com/app/installations/77/access_tokens"
    bearer = seen["auth"].removeprefix("Bearer ")
    assert jwt.decode(bearer, public, algorithms=["RS256"])["iss"] == "12345"
