"""Travellers' logins (issue 1.5): adding a traveller makes their Cognito account and a setup code for the phone.
Cognito is faked by moto (a pool shaped like cloudformation/careroute-cognito.yaml)."""
import re

import boto3
import pytest
from fastapi.testclient import TestClient

import app.main as service
from app.config import settings
from app.logins import TravellerLogins, setup_code
from tests.conftest import FAKE_AWS, new_caregiver

pytestmark = pytest.mark.skipif(not FAKE_AWS, reason="needs moto's Cognito (LocalStack's free edition has none)")


@pytest.fixture
def pool():
    cognito = boto3.client("cognito-idp", region_name=settings.aws_region)
    pool_id = cognito.create_user_pool(
        PoolName="careroute-test", AliasAttributes=["email"],
        Schema=[{"Name": "userId", "AttributeDataType": "String", "Mutable": True}],
        Policies={"PasswordPolicy": {"MinimumLength": 8, "RequireLowercase": True, "RequireNumbers": True,
                                     "RequireUppercase": False, "RequireSymbols": False}},
    )["UserPool"]["Id"]
    cognito.create_group(UserPoolId=pool_id, GroupName="traveller")
    client_id = cognito.create_user_pool_client(UserPoolId=pool_id, ClientName="web",
                                                ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH",
                                                                   "ALLOW_REFRESH_TOKEN_AUTH"])["UserPoolClient"]["ClientId"]
    yield cognito, pool_id, client_id
    cognito.delete_user_pool(UserPoolId=pool_id)


@pytest.fixture
def client(dynamodb, pool, monkeypatch):
    """The service with logins on (as with AUTH_MODE=cognito); who is calling still comes from X-Dev-User."""
    cognito, pool_id, _ = pool
    monkeypatch.setattr(service, "logins", TravellerLogins(cognito, pool_id))
    return TestClient(service.app)


def as_(caregiver_id: str) -> dict:
    return {"X-Dev-User": caregiver_id}


def signs_in(pool, username: str, password: str) -> bool:
    cognito, _, client_id = pool
    try:
        cognito.initiate_auth(ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
                              AuthParameters={"USERNAME": username, "PASSWORD": password})
        return True
    except cognito.exceptions.NotAuthorizedException:
        return False


def test_setup_codes_are_easy_to_type_and_pass_the_password_policy():
    for _ in range(200):
        code = setup_code()
        assert re.fullmatch(r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}", code)
        assert re.search(r"\d", code) and re.search(r"[a-z]", code)
        assert not set(code) & set("01oil")


def test_adding_a_traveller_makes_their_login(client, pool):
    cognito, pool_id, _ = pool
    traveller = client.post("/api/travellers", json={"name": "Alex"}, headers=as_(new_caregiver())).json()

    login = traveller["login"]
    assert login["username"] == traveller["travellerId"]
    assert signs_in(pool, login["username"], login["setupCode"])   # no "choose a new password" step
    user = cognito.admin_get_user(UserPoolId=pool_id, Username=login["username"])
    assert {"Name": "custom:userId", "Value": traveller["travellerId"]} in user["UserAttributes"]
    groups = cognito.admin_list_groups_for_user(UserPoolId=pool_id, Username=login["username"])["Groups"]
    assert [g["GroupName"] for g in groups] == ["traveller"]


def test_a_new_setup_code_replaces_the_old_one(client, pool):
    me = new_caregiver()
    old = client.post("/api/travellers", json={"name": "Alex"}, headers=as_(me)).json()["login"]

    resp = client.post(f"/api/travellers/{old['username']}/login", headers=as_(me))

    assert resp.status_code == 200
    new = resp.json()
    assert new["username"] == old["username"] and new["setupCode"] != old["setupCode"]
    assert signs_in(pool, new["username"], new["setupCode"])
    assert not signs_in(pool, old["username"], old["setupCode"])


def test_a_setup_code_is_made_for_a_traveller_added_before_logins_existed(client, pool):
    me = new_caregiver()
    traveller_id = service.profiles.create_traveller(me, "Alex", {})["travellerId"]   # no Cognito account yet

    login = client.post(f"/api/travellers/{traveller_id}/login", headers=as_(me)).json()

    assert signs_in(pool, traveller_id, login["setupCode"])


def test_only_their_caregivers_can_make_a_setup_code(client):
    traveller_id = client.post("/api/travellers", json={"name": "Alex"}, headers=as_(new_caregiver())).json()["travellerId"]

    assert client.post(f"/api/travellers/{traveller_id}/login", headers=as_(new_caregiver())).status_code == 404
    assert client.post(f"/api/travellers/{traveller_id}/login", headers=as_(traveller_id)).status_code == 403


def test_removing_the_last_caregiver_deletes_the_login(client, pool):
    cognito, pool_id, _ = pool
    me = new_caregiver()
    traveller_id = client.post("/api/travellers", json={"name": "Alex"}, headers=as_(me)).json()["travellerId"]

    assert client.delete(f"/api/travellers/{traveller_id}", headers=as_(me)).json()["profileDeleted"] is True

    with pytest.raises(cognito.exceptions.UserNotFoundException):
        cognito.admin_get_user(UserPoolId=pool_id, Username=traveller_id)


def test_the_traveller_is_still_added_if_cognito_fails(client, monkeypatch, pool):
    cognito, _, _ = pool
    monkeypatch.setattr(service, "logins", TravellerLogins(cognito, "ap-southeast-1_doesnotexist"))
    me = new_caregiver()

    resp = client.post("/api/travellers", json={"name": "Alex"}, headers=as_(me))

    assert resp.status_code == 201 and resp.json()["login"] is None
    assert resp.json()["travellerId"] in [t["travellerId"] for t in client.get("/api/travellers", headers=as_(me)).json()]
    again = client.post(f"/api/travellers/{resp.json()['travellerId']}/login", headers=as_(me))
    assert again.status_code == 503


def test_without_cognito_there_are_no_traveller_logins(dynamodb, monkeypatch):
    monkeypatch.setattr(service, "logins", None)
    client = TestClient(service.app)
    me = new_caregiver()
    traveller = client.post("/api/travellers", json={"name": "Alex"}, headers=as_(me)).json()

    assert "login" not in traveller
    resp = client.post(f"/api/travellers/{traveller['travellerId']}/login", headers=as_(me))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "logins_off"
