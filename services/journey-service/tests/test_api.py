"""The HTTP API: planning and saved routes."""
import respx
from fastapi.testclient import TestClient

from helpers import make_app, mock_lta, mock_onemap, route_data


PLAN_BODY = {"from": {"lat": 1.37, "lng": 103.8486}, "to": {"lat": 1.3326, "lng": 103.8474}}


@respx.mock
def test_plan_endpoint():
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta()
    with TestClient(make_app()) as c:
        resp = c.post("/api/routes/plan", json={**PLAN_BODY, "departAt": "2026-10-22T08:00:00"})
    assert resp.status_code == 200
    options = resp.json()["options"]
    assert options and options[0]["steps"][0]["mode"] == "walk"


def test_route_lifecycle_over_http():
    with TestClient(make_app()) as c:
        created = c.post("/api/routes", json=route_data())
        assert created.status_code == 201
        route = created.json()
        rid = route["routeId"]
        assert (route["version"], route["approved"]) == (1, False)

        assert c.get(f"/api/routes/{rid}").json() == route

        edited = c.put(f"/api/routes/{rid}", json={**route_data(name="Home to GP"), "version": 1})
        assert edited.status_code == 200
        assert (edited.json()["version"], edited.json()["name"]) == (2, "Home to GP")

        approved = c.post(f"/api/routes/{rid}/approve")
        assert approved.status_code == 200
        assert (approved.json()["approved"], approved.json()["version"]) == (True, 2)

        listed = c.get("/api/routes", params={"travellerId": "U-042"}).json()["routes"]
        assert [r["routeId"] for r in listed] == [rid] and listed[0]["approved"] is True
