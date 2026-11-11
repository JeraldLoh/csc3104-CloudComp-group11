"""Who may send check-ins (issue 1.5): with AUTH_MODE=cognito, only the traveller, with a valid token.
Tokens are signed here with a throwaway key; AWS and Redis are faked as in test_checkins.py."""
import dataclasses
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.auth import Authenticator
from app.config import Settings
from app.main import create_app
from tests.test_checkins import checkin, env  # noqa: F401  (fixture)

ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_test"
COGNITO = dataclasses.replace(Settings(), reconciler_enabled=False, auth_mode="cognito", cognito_region="ap-southeast-1",
                              cognito_user_pool_id="ap-southeast-1_test", cognito_client_id="web")


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key, user_id="U-042", group="traveller", **claims) -> str:
    body = {"iss": ISSUER, "aud": "web", "exp": int(time.time()) + 300, "token_use": "id",
            "custom:userId": user_id, "cognito:groups": [group], **claims}
    return jwt.encode(body, key, algorithm="RS256")


@pytest.fixture
def client(env, key):  # noqa: F811
    auth = Authenticator(COGNITO, key_resolver=lambda _token: key.public_key())
    with TestClient(create_app(service=env.service, s=COGNITO, authenticator=auth), raise_server_exceptions=False) as c:
        yield c


def bearer(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def batch() -> dict:
    one = checkin(1)
    return {"tripId": one["tripId"], "travellerId": one["travellerId"],
            "checkins": [{k: v for k, v in checkin(seq).items() if k not in ("tripId", "travellerId")} for seq in (1, 2)]}


def test_the_traveller_sends_their_own_checkins(client, key):
    assert client.post("/api/checkins", json=checkin(1), headers=bearer(token(key))).status_code == 202
    assert client.post("/api/checkins/batch", json=batch(), headers=bearer(token(key))).status_code == 202


def test_no_token_is_401(client):
    resp = client.post("/api/checkins", json=checkin(1))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("claims", [
    {"exp": int(time.time()) - 10},              # expired
    {"aud": "another-app"},                      # issued to another app
    {"iss": "https://example.com/not-our-pool"}, # another user pool
])
def test_a_bad_token_is_401(client, key, claims):
    assert client.post("/api/checkins", json=checkin(1), headers=bearer(token(key, **claims))).status_code == 401


def test_a_token_signed_by_someone_else_is_401(client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    assert client.post("/api/checkins", json=checkin(1), headers=bearer(token(other))).status_code == 401


@pytest.mark.parametrize("user_id,group", [("U-SOMEONEELSE", "traveller"), ("G-003", "caregiver")])
def test_nobody_else_sends_their_checkins(client, key, user_id, group):
    t = token(key, user_id, group)

    one = client.post("/api/checkins", json=checkin(1), headers=bearer(t))
    many = client.post("/api/checkins/batch", json=batch(), headers=bearer(t))

    assert one.status_code == many.status_code == 403
    assert one.json()["error"]["code"] == "forbidden"


def test_a_refused_checkin_is_not_stored(client, env, key):  # noqa: F811
    client.post("/api/checkins", json=checkin(1), headers=bearer(token(key, "U-SOMEONEELSE")))

    assert env.messages() == []


def test_off_checks_nobody(env):  # noqa: F811
    # The env fixture's app runs with the default AUTH_MODE=off
    assert env.client.post("/api/checkins", json=checkin(1)).status_code == 202


def test_cognito_needs_its_settings(env):  # noqa: F811
    with pytest.raises(RuntimeError, match="COGNITO_USER_POOL_ID"):
        create_app(service=env.service, s=dataclasses.replace(COGNITO, cognito_user_pool_id=""))
