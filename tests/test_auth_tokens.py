import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core import auth
from app.core.config import Settings
from app.core.errors import AppError

URL = "https://example.supabase.co"
ISS = f"{URL}/auth/v1"
SECRET = "test-secret-that-is-long-enough-for-hs256-signing"


def make_claims(**overrides):
    now = int(time.time())
    claims = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "aud": "authenticated",
        "iss": ISS,
        "exp": now + 300,
        "iat": now,
        "aal": "aal1",
    }
    claims.update(overrides)
    return claims


def secret_settings() -> Settings:
    return Settings(
        supabase_url=URL, supabase_jwt_mode="secret", supabase_jwt_secret=SECRET, database_url=None
    )


def test_secret_mode_accepts_valid_token():
    token = jwt.encode(make_claims(), SECRET, algorithm="HS256")
    assert auth.verify_token(token, secret_settings())["sub"].startswith("1111")


@pytest.mark.parametrize(
    "claims, key",
    [
        (make_claims(exp=int(time.time()) - 10), SECRET),  # expired
        (make_claims(aud="anon"), SECRET),  # wrong audience
        (make_claims(iss="https://other.supabase.co/auth/v1"), SECRET),  # wrong issuer
        (make_claims(), "a-different-secret-that-is-also-long-enough"),  # bad signature
    ],
)
def test_secret_mode_rejects_bad_tokens(claims, key):
    token = jwt.encode(claims, key, algorithm="HS256")
    with pytest.raises(AppError) as err:
        auth.verify_token(token, secret_settings())
    assert err.value.status == 401 and err.value.code == "UNAUTHENTICATED"


def test_secret_mode_without_secret_is_a_server_error():
    settings = Settings(supabase_url=URL, supabase_jwt_mode="secret", database_url=None)
    with pytest.raises(AppError) as err:
        auth.verify_token("x.y.z", settings)
    assert err.value.code == "AUTH_NOT_CONFIGURED"


def test_jwks_mode_verifies_with_project_key(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class FakeJwks:
        def get_signing_key_from_jwt(self, _token):
            return type("Key", (), {"key": private.public_key()})()

    monkeypatch.setattr(auth, "_jwks_client", lambda _url: FakeJwks())
    settings = Settings(supabase_url=URL, supabase_jwt_mode="jwks", database_url=None)

    good = jwt.encode(make_claims(), private, algorithm="RS256")
    assert auth.verify_token(good, settings)["aud"] == "authenticated"

    forged = jwt.encode(make_claims(), other, algorithm="RS256")
    with pytest.raises(AppError) as err:
        auth.verify_token(forged, settings)
    assert err.value.code == "UNAUTHENTICATED"


def test_hs256_token_is_refused_in_jwks_mode(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class FakeJwks:
        def get_signing_key_from_jwt(self, _token):
            return type("Key", (), {"key": private.public_key()})()

    monkeypatch.setattr(auth, "_jwks_client", lambda _url: FakeJwks())
    settings = Settings(supabase_url=URL, supabase_jwt_mode="jwks", database_url=None)
    token = jwt.encode(make_claims(), SECRET, algorithm="HS256")
    with pytest.raises(AppError):
        auth.verify_token(token, settings)
