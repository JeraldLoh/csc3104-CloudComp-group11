"""Who is calling: Cognito logins, dev mode, and who may see or change which route and trip."""
import base64
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.auth import Authenticator, Caller, Unauthorized
from app.config import Settings
from helpers import make_app, route_data


POOL = dict(cognito_region="ap-southeast-1", cognito_user_pool_id="ap-southeast-1_TESTPOOL",
            cognito_client_id="client-123")
ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_TESTPOOL"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)           # the pool's signing key
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)     # an attacker's key


def cognito_settings(**over) -> Settings:
    return Settings(lta_account_key="lta", auth_mode="cognito", **POOL, **over)


def mint(user="G-003", groups=("caregiver",), key=KEY, kid="k1", **claims) -> str:
    """A Cognito-style ID token. A claim set to None is left out."""
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": "client-123", "token_use": "id", "sub": "a1b2", "iat": now, "exp": now + 3600,
               "custom:userId": user, "cognito:groups": list(groups) if groups is not None else None, **claims}
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, key, algorithm="RS256", headers={"kid": kid})


def verifier(**over) -> Authenticator:
    return Authenticator(cognito_settings(**over), key_resolver=lambda token: KEY.public_key())


def bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


def test_valid_caregiver_and_traveller_tokens():
    auth = verifier()
    assert auth.authenticate(bearer(mint("G-003", ["caregiver"]))) == Caller("G-003", "caregiver")
    assert auth.authenticate(bearer(mint("U-042", ["traveller"]))) == Caller("U-042", "traveller")
    assert auth.authenticate(bearer(mint("G-003", ["caregiver", "other-group"]))) == Caller("G-003", "caregiver")
    assert auth.authenticate({"authorization": "bearer " + mint()}) == Caller("G-003", "caregiver")   # scheme is case-insensitive


@pytest.mark.parametrize("why,token", [
    ("expired", mint(exp=int(time.time()) - 60)),
    ("not valid yet", mint(nbf=int(time.time()) + 3600)),
    ("no expiry", mint(exp=None)),
    ("wrong issuer", mint(iss="https://cognito-idp.ap-southeast-1.amazonaws.com/another-pool")),
    ("wrong audience", mint(aud="another-app")),
    ("no audience", mint(aud=None)),
    ("access token where an ID token is expected", mint(token_use="access")),
    ("signed with someone else's key", mint(key=OTHER_KEY)),
    ("no user id", mint(user=None)),
    ("user id in the wrong format", mint("admin")),
    ("user id with extra characters", mint("G-003; DROP TABLE")),
    ("no groups", mint(groups=None)),
    ("groups that mean nothing here", mint(groups=["admins"])),
    ("both roles", mint(groups=["caregiver", "traveller"])),
    ("caregiver id on a traveller account", mint("G-003", ["traveller"])),
    ("traveller id on a caregiver account", mint("U-042", ["caregiver"])),
    ("groups of the wrong type", mint(**{"cognito:groups": 7})),
    ("garbage", "not.a.token"),
])
def test_bad_tokens_are_refused(why, token):
    with pytest.raises(Unauthorized):
        verifier().authenticate(bearer(token))


def test_unsigned_and_algorithm_confusion_tokens_are_refused():
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    payload = b64(json.dumps({"iss": ISSUER, "aud": "client-123", "token_use": "id", "exp": int(time.time()) + 3600,
                              "custom:userId": "G-003", "cognito:groups": ["caregiver"]}).encode())
    unsigned = b64(b'{"alg":"none","typ":"JWT"}') + "." + payload + "."
    header = b64(b'{"alg":"HS256","typ":"JWT"}')
    public_pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    hs256 = f"{header}.{payload}.{b64(hmac.new(public_pem, f'{header}.{payload}'.encode(), 'sha256').digest())}"
    for token in (unsigned, hs256):                           # the classic ways to forge a token
        with pytest.raises(Unauthorized):
            verifier().authenticate(bearer(token))


def test_dev_mode_trusts_the_header_and_off_mode_checks_nothing():
    dev = Authenticator(Settings(lta_account_key="lta", auth_mode="dev"))
    assert dev.authenticate({"x-dev-user": "G-1"}) == Caller("G-1", "caregiver")
    assert dev.authenticate({"x-dev-user": "U-1"}) == Caller("U-1", "traveller")
    for bad in ({}, {"x-dev-user": ""}, {"x-dev-user": "admin"}, {"x-dev-user": "G-"}, {"x-dev-user": "X-1"}):
        with pytest.raises(Unauthorized):
            dev.authenticate(bad)
    off = Authenticator(Settings(lta_account_key="lta"))
    assert off.authenticate({}) is None and off.authenticate({"authorization": "garbage"}) is None
    assert not off.enabled and dev.enabled


def secured_app(**over):
    return make_app(settings=Settings(lta_account_key="lta", onemap_api_key="om", auth_mode="dev", **over))


def as_user(user: str) -> dict:
    return {"x-dev-user": user}


class World:
    """Caregiver G-1 looks after traveller U-1: an approved route and a planned trip, all made through the API."""
    def __init__(self, c):
        self.c = c
        body = route_data(travellerId="U-1", caregiverId="G-1")
        created = c.post("/api/routes", json=body, headers=as_user("G-1"))
        assert created.status_code == 201, created.text
        self.route_id = created.json()["routeId"]
        assert c.post(f"/api/routes/{self.route_id}/approve", headers=as_user("G-1")).status_code == 200
        trip = c.post("/api/trips", json={"routeId": self.route_id, "date": "2026-10-22"}, headers=as_user("G-1"))
        assert trip.status_code == 201, trip.text
        self.trip_id = trip.json()["tripId"]

    def call(self, user, method, path, **kw):
        return getattr(self.c, method)(path, headers=as_user(user), **kw)

    def route_calls(self, user):
        """Status code of every route and trip action as `user`. Order matters: editing the route comes last,
        because it changes the version and so invalidates the trip for everything after it."""
        r, t = f"/api/routes/{self.route_id}", f"/api/trips/{self.trip_id}"
        edit = {**route_data(travellerId="U-1", caregiverId="G-1", name="edited"), "version": 1}
        return {
            "get route": self.call(user, "get", r).status_code,
            "get trip": self.call(user, "get", t).status_code,
            "pack": self.call(user, "get", f"{t}/pack").status_code,
            "start trip": self.call(user, "post", f"{t}/start").status_code,
            "end trip": self.call(user, "post", f"{t}/end").status_code,
            "new trip": self.call(user, "post", "/api/trips", json={"routeId": self.route_id, "date": "2026-10-23"}).status_code,
            "approve route": self.call(user, "post", f"{r}/approve").status_code,
            "edit route": self.call(user, "put", r, json=edit).status_code,
        }


        # (pack is 503 only because this test app has no S3: the caregiver is allowed, storage is missing)


def test_the_traveller_can_read_and_run_their_own_trip_but_not_change_the_route():
    with TestClient(secured_app()) as c:
        w = World(c)
        assert w.route_calls("U-1") == {"get route": 200, "get trip": 200, "pack": 503, "start trip": 200,
                                        "end trip": 200, "new trip": 403, "approve route": 403, "edit route": 403}
        refused = w.call("U-1", "put", f"/api/routes/{w.route_id}",
                         json={**route_data(travellerId="U-1", caregiverId="G-1"), "version": 1})
        assert refused.json()["error"]["code"] == "forbidden"
        assert w.call("G-1", "get", f"/api/routes/{w.route_id}").json()["version"] == 1       # nothing was changed


def test_only_the_routes_caregiver_can_delete_it():
    with TestClient(secured_app()) as c:
        w = World(c)
        r = f"/api/routes/{w.route_id}"
        assert w.call("U-1", "delete", r).status_code == 403          # the traveller may read, not delete
        assert w.call("G-2", "delete", r).status_code == 404          # a stranger can't tell it exists
        assert w.call("G-1", "get", r).status_code == 200
        assert w.call("G-1", "delete", r, params={"version": 7}).status_code == 409
        done = w.call("G-1", "delete", r, params={"version": 1})
        assert done.status_code == 204 and done.content == b""
        assert w.call("G-1", "get", r).status_code == 404


@pytest.mark.parametrize("stranger", ["G-2", "U-2"])
def test_a_stranger_gets_not_found_for_everything_and_changes_nothing(stranger):
    with TestClient(secured_app()) as c:
        w = World(c)
        got = w.route_calls(stranger)
        assert set(got.values()) == {404}, got
        assert w.call("G-1", "get", f"/api/routes/{w.route_id}").json()["version"] == 1       # untouched
        assert w.call("G-1", "get", f"/api/routes/{w.route_id}").json()["approved"] is True
        assert w.call("G-1", "get", f"/api/trips/{w.trip_id}").json()["status"] == "planned"
