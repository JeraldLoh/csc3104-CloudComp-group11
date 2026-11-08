"""Bridging buses during a train breakdown (bridge.py) and changing a trip under way (journey_client "trip changes"):
proposed, held for the caregiver, sent by itself after BRIDGE_AUTO_SEND_S, or a whole new route from the traveller."""
import json
from datetime import timedelta, timezone

import fakeredis
import httpx
import pytest
import respx

from app.auth import Caller, Forbidden
from app.bridge import affected_step, bridge_for, splice
from app.bridge import through as bridge_through
from app.easy_read import route_wording
from app.journey_client import JourneyClient
from app.trip_store import TripConflict, TripNotFound
from helpers import LTA, NOW, SETTINGS, make, mock_lta, mock_onemap, mock_station_search

NAMES = {17: "Punggol", 16: "Sengkang", 15: "Buangkok", 14: "Hougang", 13: "Kovan", 12: "Serangoon", 6: "Dhoby Ghaut"}
RIDE = [{"code": f"NE{n}", "name": f"{name} MRT Station"} for n, name in NAMES.items()]
HOME = {"name": "Home", "code": None, "lat": 1.4040, "lng": 103.9010}
PUNGGOL = {**RIDE[0], "lat": 1.4052, "lng": 103.9024}
DHOBY = {**RIDE[-1], "lat": 1.2990, "lng": 103.8455}
TRAIN = {"stepIndex": 1, "mode": "train", "line": "NE", "instruction": "Take the North East Line (6 stops)",
         "from": PUNGGOL, "to": DHOBY, "stops": RIDE, "stopCount": 6, "expectedDurationS": 1200}
WALK = {"stepIndex": 0, "mode": "walk", "instruction": "Walk to Punggol", "from": HOME, "to": PUNGGOL,
        "expectedDurationS": 240}
SHUTTLE = {"line": "NEL", "stations": ["NE13", "NE14", "NE15"], "freeMrtShuttle": ["NE12", "NE13", "NE14", "NE15", "NE16"],
           "freePublicBus": [], "shuttleDirection": "Both"}


# ----- the bridge on its own -----

def test_bridging_bus_from_the_station_before_the_stretch_to_the_one_after():
    made = bridge_for(TRAIN, SHUTTLE)
    assert made["kind"] == "shuttle"
    assert (made["leaveAt"]["name"], made["target"]["name"]) == ("Sengkang MRT Station", "Serangoon MRT Station")
    before, bus, after = made["steps"]
    assert [s["code"] for s in before["stops"]] == ["NE17", "NE16"] and before["stopCount"] == 1
    assert bus["bridge"] == {"kind": "shuttle", "line": "NE"} and bus["mode"] == "bus"
    assert [s["code"] for s in after["stops"]] == ["NE12", "NE6"]


def test_bridge_ends_inside_the_stretch_when_the_bus_doesnt_call_outside_it():
    made = bridge_for(TRAIN, {**SHUTTLE, "freeMrtShuttle": ["NE13", "NE14", "NE15"]})
    assert (made["leaveAt"]["code"], made["target"]["code"]) == ("NE15", "NE13")


def test_no_bridge_the_other_way_round_or_without_free_buses():
    assert bridge_for(TRAIN, {**SHUTTLE, "shuttleDirection": "Punggol Coast"}) is None      # buses only go north
    assert bridge_for(TRAIN, {**SHUTTLE, "shuttleDirection": "HarbourFront"})["kind"] == "shuttle"
    assert bridge_for(TRAIN, {**SHUTTLE, "freeMrtShuttle": []}) is None
    assert bridge_for(TRAIN, {**SHUTTLE, "stations": ["CC1"]}) is None                       # not on this ride


def test_free_public_bus_when_there_is_no_bridging_bus():
    asked = []

    def free_bus(leave, target):
        asked.append((leave["code"], target["code"]))
        return {"serviceNo": "72", "board": {"code": "67009", "name": "Sengkang Int"}, "alight": {"code": "64009", "name": "Opp Serangoon Stn"}}
    made = bridge_for(TRAIN, {**SHUTTLE, "freeMrtShuttle": [], "freePublicBus": SHUTTLE["freeMrtShuttle"]}, free_bus)
    assert made["kind"] == "bus" and asked == [("NE16", "NE12")]
    text = route_wording([TRAIN, *made["steps"]])[2]
    assert text == ("The purple line is not running here. Leave the station. Find bus stop 67009 at Sengkang Interchange. "
                    "Raise your hand for bus 72. It is free today. You do not need to tap your card. "
                    "Get off at Opposite Serangoon Station. Walk to Serangoon MRT Station.")


def test_bridge_wording_and_splice():
    plan = [WALK, TRAIN]
    index = affected_step(plan, 0, SHUTTLE)
    spliced = splice(plan, index, index, bridge_for(TRAIN, SHUTTLE)["steps"])
    assert [s["stepIndex"] for s in spliced] == [0, 1, 2, 3] and spliced[0] is not WALK and spliced[0]["mode"] == "walk"
    words = route_wording(spliced, "Orchard")
    assert words[1].endswith("Get off at Sengkang MRT Station. It is the 1st stop.")
    assert words[2] == ("The purple line is not running here. Leave the station. Follow the signs for the free "
                        "bridging bus. Take it to Serangoon MRT Station. You do not need to pay. Get off at Serangoon MRT Station.")
    assert words[3].startswith("Tap your card at the gantry.")                     # back into the trains
    assert affected_step(plan, 2, SHUTTLE) is None                                 # already past it


# ----- a trip under way -----

class Trip:
    def __init__(self, alert=SHUTTLE):
        self.r = fakeredis.FakeRedis()
        self.told = []
        self.j = JourneyClient(SETTINGS, redis_client=self.r, sleep=lambda _: None, clock=lambda: NOW,
                               notify=self.told.append)
        data = {"travellerId": "U-042", "caregiverId": "G-003", "name": "Home to Dhoby Ghaut",
                "origin": {"name": "Home", "lat": HOME["lat"], "lng": HOME["lng"]},
                "destination": {"name": "Plaza Singapura", "lat": 1.3005, "lng": 103.8452},
                "steps": [WALK, TRAIN], "path": [[1.404, 103.901], [1.299, 103.8455]]}
        route = self.j.create_route(data)
        self.j.approve_route(route["routeId"])
        self.trip = self.j.create_trip(route["routeId"], "2026-10-22")
        self.j.start_trip(self.trip["tripId"])
        self.id = self.trip["tripId"]
        self.alert(alert)

    def alert(self, segment):
        self.j.lta._cache._r = self.r
        self.r.delete("journey:cache:train-alerts")
        raw = {"value": {"Status": 2 if segment else 1, "Message": [], "AffectedSegments": [] if not segment else [{
            "Line": segment["line"], "Direction": "Both", "Stations": ",".join(segment["stations"]),
            "FreeMRTShuttle": ",".join(segment["freeMrtShuttle"]), "FreePublicBus": ",".join(segment["freePublicBus"]),
            "MRTShuttleDirection": segment["shuttleDirection"]}]}}
        respx.get(f"{LTA}/TrainServiceAlerts").mock(return_value=httpx.Response(200, json=raw))
        # Where a station is that the route only knows by name (OneMap's exit search): not known here.
        respx.get(f"{SETTINGS.onemap_base_url}/api/common/elastic/search").mock(
            return_value=httpx.Response(200, json={"found": 0, "totalNumPages": 1, "results": []}))

    def get(self):
        return self.j.get_trip(self.id)

    def pack(self):
        return self.j.build_pack(self.id)


@respx.mock
def test_proposed_then_sent_by_itself_after_three_minutes():
    t = Trip()
    assert t.j.watch_bridges(NOW) == {"proposed": 1, "sent": 0, "withdrawn": 0}
    offer = t.get()["override"]
    assert (offer["kind"], offer["status"], offer["leaveAt"], offer["target"]) == (
        "bridge", "pending", "Sengkang MRT Station", "Serangoon MRT Station")
    assert "free bridging bus" in offer["steps"][1]["easyRead"]
    assert t.pack()["packVersion"].endswith("-v1") and len(t.pack()["steps"]) == 2      # not sent yet
    assert "will be sent to them at 08:03" in t.told[0]["message"] and t.told[0]["tripId"] == t.id
    assert t.j.watch_bridges(NOW + timedelta(seconds=60)) == {"proposed": 0, "sent": 0, "withdrawn": 0}
    assert t.j.watch_bridges(NOW + timedelta(seconds=180))["sent"] == 1
    trip = t.get()
    assert (trip["override"]["status"], trip["override"]["sentBy"], trip["planVersion"]) == ("sent", "auto", 2)
    pack = t.pack()
    assert pack["packVersion"].endswith("-v1-b2") and [s["mode"] for s in pack["steps"]] == ["walk", "train", "bus", "train"]
    cached = json.loads(t.r.get(f"trip:{t.id}:route"))
    assert [s["mode"] for s in cached["steps"]] == ["walk", "train", "bus", "train"]     # the detectors follow it
    assert "automatically" in t.told[-1]["message"]
    assert t.j.watch_bridges(NOW + timedelta(seconds=300))["proposed"] == 0          # the new plan avoids the stretch


@respx.mock
def test_caregiver_sends_it_now_or_chooses_not_to():
    t = Trip()
    t.j.watch_bridges(NOW)
    sent = t.j.send_bridge(t.id, Caller("G-003", "caregiver"))
    assert (sent["override"]["sentBy"], sent["planVersion"]) == ("caregiver", 2) and "planSteps" not in sent
    with pytest.raises(TripConflict):
        t.j.send_bridge(t.id)                                                       # nothing waiting now

    t = Trip()
    t.j.watch_bridges(NOW)
    with pytest.raises(Forbidden):
        t.j.dismiss_bridge(t.id, Caller("U-042", "traveller"))                      # sees the trip, can't change it
    with pytest.raises(TripNotFound):
        t.j.dismiss_bridge(t.id, Caller("G-999", "caregiver"))                      # not theirs: not found
    assert t.j.dismiss_bridge(t.id)["override"]["status"] == "dismissed"
    assert t.j.watch_bridges(NOW + timedelta(seconds=600)) == {"proposed": 0, "sent": 0, "withdrawn": 0}
    assert t.get()["planVersion"] == 1
    reopened = t.j.send_bridge(t.id)                                                # changed their mind
    assert (reopened["override"]["status"], reopened["planVersion"]) == ("sent", 2)


@respx.mock
def test_a_dismissed_bridge_goes_when_the_breakdown_ends():
    t = Trip()
    t.j.watch_bridges(NOW)
    t.j.dismiss_bridge(t.id)
    t.alert(None)
    assert t.j.watch_bridges(NOW + timedelta(seconds=60))["withdrawn"] == 1
    with pytest.raises(TripConflict):
        t.j.send_bridge(t.id)


@respx.mock
def test_a_trip_not_started_can_use_the_bridging_bus_before_it_starts():
    t = Trip()
    planned = t.j.create_trip(t.trip["routeId"], "2026-10-23")["tripId"]
    seen = t.j.bridge_preview(planned, Caller("G-003", "caregiver"))
    assert (seen["leaveAt"], seen["target"]) == ("Sengkang MRT Station", "Serangoon MRT Station")
    assert [s["mode"] for s in seen["steps"]] == ["train", "bus", "train"] and all(s["easyRead"] for s in seen["steps"])
    assert t.j.get_trip(planned)["planVersion"] == 1                                # only a look
    used = t.j.use_bridge(planned, Caller("G-003", "caregiver"))
    assert (used["status"], used["override"]["kind"], used["override"]["sentBy"], used["planVersion"]) == (
        "planned", "bridge", "caregiver", 2)
    assert planned.encode() not in t.r.smembers("active-trips") and not t.told[1:]  # nothing to announce yet
    t.j.start_trip(planned)
    assert [s["mode"] for s in json.loads(t.r.get(f"trip:{planned}:route"))["steps"]] == ["walk", "train", "bus", "train"]
    t.alert(None)
    with pytest.raises(TripConflict):
        t.j.use_bridge(t.id)                                                        # no breakdown: nothing to use


@respx.mock
def test_withdrawn_when_the_breakdown_ends_or_has_no_free_bus():
    t = Trip()
    t.j.watch_bridges(NOW)
    t.alert(None)
    assert t.j.watch_bridges(NOW + timedelta(seconds=30))["withdrawn"] == 1
    assert t.get()["override"]["status"] == "withdrawn"
    t = Trip({**SHUTTLE, "freeMrtShuttle": []})
    assert t.j.watch_bridges(NOW)["proposed"] == 0 and t.get()["override"] is None


@respx.mock
def test_traveller_past_the_breakdown_gets_nothing():
    t = Trip()
    t.r.hset(f"trip:{t.id}:context", "stepIndex", 2)
    assert t.j.watch_bridges(NOW)["proposed"] == 0


# ----- a whole new route from where the traveller is -----

def test_replan_starts_at_a_recent_check_in_else_at_the_current_step():
    t = Trip.__new__(Trip)
    t.r = fakeredis.FakeRedis()
    j = JourneyClient(SETTINGS, redis_client=t.r, clock=lambda: NOW)
    plan = [WALK, TRAIN]
    t.r.hset("trip:T-1:context", "stepIndex", 1)
    assert j.replan_origin("T-1", plan)["source"] == "step"
    assert (j.replan_origin("T-1", plan)["lat"], j.replan_origin("T-1", plan)["lng"]) == (PUNGGOL["lat"], PUNGGOL["lng"])
    fresh = {"lat": 1.39, "lng": 103.89, "timestamp": (NOW - timedelta(minutes=2)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    t.r.lpush("trip:T-1:recent", json.dumps({**fresh, "timestamp": "2026-10-21T00:00:00Z"}), json.dumps(fresh))
    assert j.replan_origin("T-1", plan)["source"] == "checkin"
    t.r.lpush("trip:T-1:recent", json.dumps({**fresh, "timestamp": (NOW - timedelta(minutes=9)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}))
    assert j.replan_origin("T-1", plan)["source"] == "step"                          # too old
    t.r.lpush("trip:T-1:recent", json.dumps({**fresh, "timestamp": (NOW + timedelta(hours=8)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}))
    assert j.replan_origin("T-1", plan)["source"] == "step"                          # from the future: a wrong clock


@respx.mock
def test_replan_replaces_the_steps_from_the_current_one_on():
    t = Trip()
    t.j.watch_bridges(NOW)                                                          # a bridging bus is waiting
    t.r.hset(f"trip:{t.id}:context", "stepIndex", 1)
    bus = {"stepIndex": 0, "mode": "bus", "line": "85", "instruction": "Take bus 85", "stopCount": 9,
           "from": {"name": "Punggol Int", "code": "65009", "lat": 1.405, "lng": 103.902},
           "to": {"name": "Opp Plaza Sing", "code": "08057", "lat": 1.300, "lng": 103.845}}
    trip = t.j.apply_replan(t.id, [bus], Caller("G-003", "caregiver"))
    assert (trip["override"]["kind"], trip["override"]["status"], trip["planVersion"]) == ("replan", "sent", 2)
    assert [s["mode"] for s in t.pack()["steps"]] == ["walk", "bus"]
    assert t.pack()["steps"][1]["easyRead"].startswith("Find bus stop 65009")
    assert "a new route" in t.told[-1]["message"]
    assert t.j.watch_bridges(NOW + timedelta(seconds=600)) == {"proposed": 0, "sent": 0, "withdrawn": 0}


@respx.mock
def test_trip_change_endpoints_over_http():
    from fastapi.testclient import TestClient
    from app.main import create_app
    t = Trip()
    t.j.watch_bridges(NOW)
    with TestClient(create_app(journey=t.j, redis_client=t.r, s=SETTINGS)) as c:
        assert c.post("/api/trips/T-000000/bridge/send").status_code == 404
        sent = c.post(f"/api/trips/{t.id}/bridge/send")
        assert sent.status_code == 200 and sent.json()["override"]["status"] == "sent"
        assert c.post(f"/api/trips/{t.id}/bridge/dismiss").status_code == 409             # nothing waiting now
        bad = c.post(f"/api/trips/{t.id}/replan/apply", json={"steps": []})
        assert bad.status_code == 422


@respx.mock
def test_location_follows_the_traveller_check_ins():
    from fastapi.testclient import TestClient
    from app.main import create_app
    t = Trip()
    t.r.hset(f"trip:{t.id}:context", "stepIndex", 1)
    before = t.j.trip_location(t.id, Caller("G-003", "caregiver"))
    assert (before["source"], before["lat"], before["trail"]) == ("step", PUNGGOL["lat"], [])     # where the step starts
    assert (before["stepIndex"], before["stepCount"], before["step"]["mode"]) == (1, 2, "train")
    stamp = lambda m: (NOW - timedelta(minutes=m)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for m, lat in ((3, 1.401), (2, 1.402), (1, 1.403)):                                         # newest ends up first
        t.r.lpush(f"trip:{t.id}:recent", json.dumps({"lat": lat, "lng": 103.9, "timestamp": stamp(m)}))
    t.r.lpush(f"trip:{t.id}:recent", "not json")
    now = t.j.trip_location(t.id, Caller("U-042", "traveller"))
    assert (now["source"], now["lat"], now["at"]) == ("checkin", 1.403, stamp(1))
    assert now["trail"] == [[1.401, 103.9], [1.402, 103.9], [1.403, 103.9]]                      # oldest first
    with pytest.raises(TripNotFound):
        t.j.trip_location(t.id, Caller("G-999", "caregiver"))
    with TestClient(create_app(journey=t.j, redis_client=t.r, s=SETTINGS)) as c:
        assert c.get(f"/api/trips/{t.id}/location").json()["lat"] == 1.403
        t.j.end_trip(t.id)
        assert c.get(f"/api/trips/{t.id}/location").status_code == 409                          # not under way


def test_through_finds_the_stretch_a_train_step_rides_with_no_trains():
    broken = set(SHUTTLE["stations"])
    first, last = bridge_through(TRAIN, broken)
    assert (first["name"], last["name"]) == ("Sengkang MRT Station", "Serangoon MRT Station")
    up_to = {**TRAIN, "stops": RIDE[:3]}                                    # Punggol .. Buangkok: ends at its edge
    from_it = {**TRAIN, "stops": RIDE[4:]}                                  # Kovan .. Dhoby Ghaut: starts at its edge
    assert bridge_through(up_to, broken) is None and bridge_through(from_it, broken) is None
    assert bridge_through(WALK, broken) is None and bridge_through(TRAIN, {"NE1"}) is None


@respx.mock
def test_trip_plan_shows_where_trains_are_not_running_and_follows_a_sent_bridge():
    t = Trip()
    where = {s["name"]: (s["lat"], s["lng"]) for s in (PUNGGOL, DHOBY)}
    where.update({"Sengkang MRT Station": (1.3917, 103.8954), "Serangoon MRT Station": (1.3502, 103.8733)})
    t.j._station_point = lambda station: where.get(station.get("name"))     # OneMap's station exits
    before = t.j.trip_plan(t.id, Caller("G-003", "caregiver"))
    assert [s["mode"] for s in before["steps"]] == ["walk", "train"] and len(before["stepPaths"]) == 2
    assert [(a["stepIndex"], a["from"], a["to"]) for a in before["affected"]] == [(1, "Sengkang MRT Station", "Serangoon MRT Station")]
    t.j.watch_bridges(NOW)
    t.j.watch_bridges(NOW + timedelta(seconds=180))                         # sent by itself
    after = t.j.trip_plan(t.id)
    assert (after["planVersion"], [s["mode"] for s in after["steps"]]) == (2, ["walk", "train", "bus", "train"])
    assert len(after["stepPaths"]) == 4 and all(after["stepPaths"])
    assert after["affected"] == []                                          # the bridge goes round it
    assert "planPaths" not in t.get()
    t.alert(None)
    t.j.end_trip(t.id)
    with pytest.raises(TripConflict):
        t.j.trip_plan(t.id)


@respx.mock
def test_replan_leaves_out_rides_through_the_breakdown():
    t = Trip()
    t.r.hset(f"trip:{t.id}:context", "stepIndex", 1)
    bus = {"stepIndex": 0, "mode": "bus", "line": "85", "instruction": "Take bus 85", "from": PUNGGOL, "to": DHOBY}

    def plan(origin, destination, **_):
        # plan() marks the options riding through the breakdown (and adds bus-only ones when it hits them all)
        return {"options": [{"option": 1, "steps": [TRAIN], "affected": [{"stepIndex": 0}]},
                            {"option": 2, "steps": [bus], "affected": []}], "sortedBy": "walking"}

    t.j.plan = plan
    found = t.j.replan_options(t.id, Caller("G-003", "caregiver"))
    assert [(o["option"], o["steps"][0]["mode"]) for o in found["options"]] == [(1, "bus")]


@respx.mock
def test_plan_through_a_breakdown_shows_the_free_bus_and_looks_for_buses_only():
    onemap = mock_onemap("route_pt_bus_mrt.json")                           # Punggol NE17 .. Dhoby Ghaut NE6
    mock_station_search()
    mock_lta(alerts={"value": {"Status": 2, "Message": [], "AffectedSegments": [{
        "Line": "NEL", "Direction": "Both", "Stations": "NE12,NE11,NE10", "FreePublicBus": "",
        "FreeMRTShuttle": "NE13,NE12,NE11,NE10,NE9", "MRTShuttleDirection": "Both"}]}})
    options = make().plan((1.37, 103.8486), (1.3326, 103.8474), NOW)["options"]
    train = next(s["stepIndex"] for s in options[0]["steps"] if s["mode"] == "train")
    assert [a["stepIndex"] for a in options[0]["affected"]] == [train]
    shown = options[0]["bridges"][0]
    assert (shown["stepIndex"], shown["kind"]) == (train, "shuttle")
    assert [s["mode"] for s in shown["steps"]] == ["train", "bus", "train"] and "bridge" not in options[0]["steps"][train]
    assert onemap.call_count == 2                                           # and again, buses only


@respx.mock
def test_a_route_saved_for_the_trip_is_sent_with_its_own_wording_and_photos():
    t = Trip()
    t.r.hset(f"trip:{t.id}:context", "stepIndex", 1)
    me = Caller("G-003", "caregiver")
    bus = {"stepIndex": 0, "mode": "bus", "line": "85", "instruction": "Take bus 85", "from": PUNGGOL, "to": DHOBY,
           "easyRead": "Take bus 85. Sit near the front.", "photoKey": "media/caregiver/U-042/bus.jpg"}
    reroute = t.j.create_route({"travellerId": "U-042", "caregiverId": "G-003", "name": f"Re-route ({t.id}): to Plaza",
                                "origin": {"name": None, **{k: PUNGGOL[k] for k in ("lat", "lng")}},
                                "destination": {"name": "Plaza Singapura", "lat": 1.3005, "lng": 103.8452},
                                "steps": [bus], "path": [[1.4052, 103.9024], [1.35, 103.87], [1.299, 103.8455]],
                                "forTripId": t.id}, me)
    assert reroute["forTripId"] == t.id
    with pytest.raises(TripConflict):
        t.j.send_route(t.id, reroute["routeId"], me)                        # not approved yet
    t.j.approve_route(reroute["routeId"], me)
    trip = t.j.send_route(t.id, reroute["routeId"], me)
    assert (trip["override"]["kind"], trip["override"]["fromRoute"], trip["planVersion"]) == ("replan", reroute["routeId"], 2)
    sent = t.j._stored_trip(t.id)["planSteps"][1]
    assert (sent["easyRead"], sent["photoKey"]) == ("Take bus 85. Sit near the front.", "media/caregiver/U-042/bus.jpg")
    assert len(t.j.trip_plan(t.id)["stepPaths"][1]) == 3                   # its own line
    with pytest.raises(Forbidden):
        t.j.send_route(t.id, reroute["routeId"], Caller("U-042", "traveller"))
