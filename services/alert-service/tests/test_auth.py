"""auth.py: the three AUTH_MODEs. Cognito tokens are signed here with a throwaway key."""
import time
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import Authenticator, Caller, Unauthorized
from app.config import settings

COGNITO = replace(settings, auth_mode="cognito", cognito_region="ap-southeast-1",
                  cognito_user_pool_id="ap-southeast-1_test", cognito_client_id="dashboard")
ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_test"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key, **claims) -> str:
    body = {"iss": ISSUER, "aud": "dashboard", "exp": int(time.time()) + 300, "token_use": "id",
            "custom:userId": "G-003", "cognito:groups": ["caregiver"], **claims}
    return jwt.encode(body, key, algorithm="RS256")


def cognito(key) -> Authenticator:
    return Authenticator(COGNITO, key_resolver=lambda _token: key.public_key())


def test_off_checks_nobody():
    assert Authenticator(replace(settings, auth_mode="off")).authenticate({}) is None


def test_dev_trusts_the_header():
    auth = Authenticator(replace(settings, auth_mode="dev"))

    assert auth.authenticate({"x-dev-user": "G-003"}) == Caller("G-003", "caregiver")
    assert auth.authenticate({"x-dev-user": "U-042"}) == Caller("U-042", "traveller")
    with pytest.raises(Unauthorized):
        auth.authenticate({})


def test_cognito_accepts_a_valid_token(key):
    caller = cognito(key).authenticate({"authorization": f"Bearer {token(key)}"})

    assert caller == Caller("G-003", "caregiver")


@pytest.mark.parametrize("claims", [
    {"exp": int(time.time()) - 10},                     # expired
    {"aud": "another-app"},                             # issued to another app
    {"iss": "https://example.com/not-our-pool"},        # another user pool
    {"cognito:groups": ["traveller"]},                  # G- id but traveller role
    {"cognito:groups": ["caregiver", "traveller"]},     # both roles
    {"token_use": "access"},                            # wrong kind of token
])
def test_cognito_rejects_bad_tokens(key, claims):
    with pytest.raises(Unauthorized):
        cognito(key).authenticate({"authorization": f"Bearer {token(key, **claims)}"})


def test_cognito_rejects_a_token_signed_by_someone_else(key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(Unauthorized):
        cognito(key).authenticate({"authorization": f"Bearer {token(other)}"})


def test_cognito_needs_its_settings():
    with pytest.raises(RuntimeError):
        Authenticator(replace(settings, auth_mode="cognito"))
