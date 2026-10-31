"""Planning: OneMap itineraries become contract steps, cross-checked against LTA live data."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.journey_client import decode_polyline, distance_to_path_m
from app.lta import SST
from helpers import (AMK, LTA, NOW, ROUTE, SEARCH, TOA_PAYOH, bus, first_leg, make, mock_lta, mock_onemap,
                     onemap_fixture, plan)


def test_decode_polyline_known_example():
    # The worked example from Google's polyline algorithm documentation.
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


def test_distance_to_path():
    path = [(1.3000, 103.8000), (1.3000, 103.8100)]          # ~1.1 km east-west line
    assert distance_to_path_m(1.3000, 103.8050, path) < 1
    assert 105 < distance_to_path_m(1.3010, 103.8050, path) < 115   # ~111 m north of it
    assert distance_to_path_m(1.3, 103.8, []) == float("inf")


def mock_search():
    return respx.get(SEARCH).mock(return_value=httpx.Response(200, json=onemap_fixture("search.json")))


@respx.mock
def test_search_returns_clean_places():
    mock_search()
    places = make().search_places("Tampines Mall")
    assert len(places) == 3
    assert places[0] == {
        "name": "Tampines Mall",
        "address": "4 Tampines Central 5 Tampines Mall Singapore 529510",
        "postalCode": "529510",
        "lat": 1.352527372785196,
        "lng": 103.9446987510717,
    }
    assert places[1]["postalCode"] is None          # OneMap's "NIL" becomes null


@respx.mock
def test_bus_itinerary_maps_to_contract_steps():
    mock_onemap("route_pt_bus_only_short.json")
    mock_lta()
    options = plan()
    raw = onemap_fixture("route_pt_bus_only_short.json")["plan"]["itineraries"]

    assert len(options) == len(raw)
    opt = options[0]
    assert opt["summary"]["durationS"] == raw[0]["duration"]
    assert opt["summary"]["startTime"].endswith("Z")
    assert len(opt["steps"]) == len(raw[0]["legs"])
    bus_steps = [s for s in opt["steps"] if s["mode"] == "bus"]
    assert bus_steps
    s = bus_steps[0]
    expected = first_leg("route_pt_bus_only_short.json", "BUS")
    assert s["line"] == expected["route"]
    assert s["from"]["code"] == expected["from"]["stopCode"]
    assert s["instruction"].startswith(f"Take bus {s['line']} from")
    assert set(s) == {"stepIndex", "mode", "instruction", "easyRead", "from", "to", "line", "stopCount", "stops",
                      "expectedDurationS", "underground", "photoKey", "audioKey", "backup"}
    assert len(s["stops"]) == s["stopCount"] + 1                     # every stop on the ride, both ends included
    assert [x["stepIndex"] for x in opt["steps"]] == list(range(len(opt["steps"])))
    assert all(1.1 < lat < 1.5 and 103.5 < lng < 104.1 for lat, lng in opt["path"])
    assert len(opt["path"]) > 5


@respx.mock
def test_mrt_itinerary_maps_to_train_step():
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta()
    opt = plan()[0]
    train = next(s for s in opt["steps"] if s["mode"] == "train")
    assert train["line"] == "NE"
    assert train["from"]["code"] == "NE17" and train["to"]["code"] == "NE6"
    assert train["from"]["name"] == "Punggol MRT Station"
    assert "North East Line" in train["instruction"]
    assert train["underground"] is None
    walk = opt["steps"][0]
    assert walk["mode"] == "walk" and walk["from"]["name"] is None     # no invented place names


@respx.mock
def test_request_to_onemap_uses_onemap_formats():
    route = mock_onemap("route_pt_bus_mrt.json")
    mock_lta()
    plan()
    p = route.calls.last.request.url.params
    assert p["routeType"] == "pt" and p["date"] == "10-22-2026" and p["time"] == "08:00:00"
    assert p["start"] == "1.37,103.8486"
    assert route.calls.last.request.headers["Authorization"] == "Bearer om"


@respx.mock
def test_bus_leg_gets_next_buses():
    leg = first_leg("route_pt_bus_only_short.json", "BUS")
    mock_onemap("route_pt_bus_only_short.json")
    mock_lta(arrival={"Services": [{"ServiceNo": leg["route"], "Operator": "SBST", "NextBus": {
        "EstimatedArrival": "2026-10-22T08:04:30+08:00", "Monitored": 1, "Latitude": "1.3",
        "Longitude": "103.8", "Load": "SDA", "Feature": "WAB", "Type": "SD"}}]})
    entry = plan()[0]["live"][0]
    assert entry["serviceNo"] == leg["route"] and entry["busStopCode"] == leg["from"]["stopCode"]
    assert entry["nextBuses"] == [{"minutes": 4, "display": "4 min", "load": "SDA", "crowd": "moderate", "wheelchairAccessible": True}]
    assert entry["crowd"] == "moderate"


@respx.mock
def test_train_disruption_on_the_leg_is_flagged():
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta(alerts={"value": {
        "Status": 2,
        "AffectedSegments": [{"Line": "NEL", "Direction": "HarbourFront", "Stations": "NE12,NE11,NE10",
                              "FreePublicBus": "", "FreeMRTShuttle": "", "MRTShuttleDirection": ""}],
        "Message": [{"Content": "NEL: no train service between Serangoon and Potong Pasir",
                     "CreatedDate": "2026-10-22 07:50:00"}]}})
    opt = plan()[0]
    w = next(w for w in opt["warnings"] if w["type"] == "train_disruption")
    train_index = next(s["stepIndex"] for s in opt["steps"] if s["mode"] == "train")
    assert w["stepIndex"] == train_index and w["severity"] == "high"
    assert w["affectedStations"] == ["NE10", "NE11", "NE12"]       # these are intermediate stops of the leg
    assert "Serangoon" in w["message"]


@respx.mock
def test_nearby_incident_flagged_and_far_incident_ignored():
    leg = first_leg("route_pt_bus_only_short.json", "BUS")
    path = decode_polyline(leg["legGeometry"]["points"])
    mid = path[len(path) // 2]
    mock_onemap("route_pt_bus_only_short.json")
    mock_lta(incidents={"value": [
        {"Type": "Accident", "Latitude": mid[0] + 0.0003, "Longitude": mid[1], "Message": "Accident near the route"},
        {"Type": "Roadwork", "Latitude": 1.45, "Longitude": 103.65, "Message": "Roadwork far away"},
    ]})
    inc = [w for w in plan()[0]["warnings"] if w["type"] == "traffic_incident"]
    assert inc and inc[0]["message"] == "Accident near the route"
    assert inc[0]["severity"] == "warning" and inc[0]["distanceM"] <= 200
    assert not [w for w in inc if "far away" in w["message"]]


@respx.mock
def test_lta_down_still_returns_options_with_warning():
    mock_onemap("route_pt_bus_only_short.json")
    for path in ("v3/BusArrival", "TrainServiceAlerts", "TrafficIncidents", "BusRoutes", "BusServices"):
        respx.get(f"{LTA}/{path}").mock(return_value=httpx.Response(503))
    options = plan()
    assert options
    assert all(any(w["type"] == "live_data_unavailable" for w in o["warnings"]) for o in options)
    assert all(o["live"] == [] for o in options)     # no arrivals known, so none are claimed


PUNGGOL_TO_ORCHARD = ((1.4043, 103.9021), (1.3040, 103.8318))        # the MRT trip (route_pt_bus_mrt.json)


def crowd_body(**levels):
    return {"value": [{"Station": s, "StartTime": "2026-10-22T08:00:00+08:00", "EndTime": "2026-10-22T08:10:00+08:00",
                       "CrowdLevel": c} for s, c in levels.items()]}


def mrt_plan(client=None, **kw):
    client = client or make()
    return client.plan(*PUNGGOL_TO_ORCHARD, NOW, **kw)["options"]


@respx.mock
def test_a_trip_that_walks_too_far_in_total_is_flagged_even_if_no_single_walk_is_long():
    mock_onemap("route_pt_bus_short.json")                       # walks 273 m + 42 m = 315 m
    mock_lta()
    flagged = lambda limit: [w for w in make().plan(AMK, TOA_PAYOH, NOW, max_walk_m=limit)["options"][0]["warnings"]
                             if w["type"] == "long_total_walk"]
    assert flagged(150) and flagged(150)[0]["stepIndex"] is None and flagged(150)[0]["distanceM"] == 320
    assert "320 m" in flagged(150)[0]["message"] and "300 m" in flagged(150)[0]["message"]
    assert not flagged(160)                                      # 315 m is not more than twice 160 m


@respx.mock
def test_a_train_leg_shows_how_busy_its_platform_is_and_the_option_takes_the_worst():
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta(crowd=crowd_body(NE17="h", NS24="m"))
    option = mrt_plan()[0]
    trains = [e for e in option["live"] if "station" in e]
    # the second train leaves about half an hour later, so only the first uses the real-time level
    assert [(e["station"], e["crowd"], e["source"]) for e in trains] == [("NE17", "high", "real-time")]
    assert option["summary"]["crowd"] == "high"
    crowded = [w for w in option["warnings"] if w["type"] == "crowded"]
    assert len(crowded) == 1 and crowded[0]["station"] == "NE17" and "very crowded now" in crowded[0]["message"]


@respx.mock
def test_a_later_train_uses_the_forecast_for_its_start_time():
    mock_onemap("route_pt_bus_mrt.json")
    leg = first_leg("route_pt_bus_mrt.json", "SUBWAY")
    start = datetime.fromtimestamp(leg["startTime"] / 1000, tz=timezone.utc).astimezone(SST)
    interval = start.replace(minute=start.minute // 30 * 30, second=0, microsecond=0).isoformat()
    forecast = {"value": [{"Date": interval, "Stations": [
        {"Station": "NE17", "Interval": [{"Start": interval, "CrowdLevel": "h"}]}]}]}
    mock_lta(forecast=forecast, crowd=crowd_body(NE17="l"))                          # real-time says quiet
    option = mrt_plan(make(clock=NOW - timedelta(hours=3)))[0]                       # but the train is hours away
    assert [(e["station"], e["crowd"], e["source"]) for e in option["live"] if "station" in e] == [
        ("NE17", "high", "forecast")]
    assert [w["message"] for w in option["warnings"] if w["type"] == "crowded"] == [
        "Punggol MRT Station is very crowded at this time."]


@respx.mock
def test_a_busy_bus_gets_a_plain_warning():
    leg = first_leg("route_pt_bus_only_short.json", "BUS")
    mock_onemap("route_pt_bus_only_short.json")
    mock_lta(arrival={"Services": [{"ServiceNo": leg["route"], "Operator": "SBST", "NextBus": bus(
        "2026-10-22T08:05:00+08:00", load="LSD")}]})
    option = plan()[0]
    assert option["live"][0]["crowd"] == "high" and option["summary"]["crowd"] == "high"
    assert f"Bus {leg['route']} is very full right now. You may have to stand." in [
        w["message"] for w in option["warnings"] if w["type"] == "crowded"]


@respx.mock
@pytest.mark.parametrize("status", [403, 404, 500])
def test_missing_crowd_data_never_breaks_planning_or_raises_the_live_data_warning(status):
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta()
    respx.get(f"{LTA}/PCDRealTime").mock(return_value=httpx.Response(status, text="nope"))
    option = mrt_plan()[0]
    assert option["steps"] and option["summary"]["crowd"] is None
    assert not [e for e in option["live"] if "station" in e]
    assert not [w for w in option["warnings"] if w["type"] in ("live_data_unavailable", "crowded")]


def ordered_punggol_buses():
    """The saved Punggol -> Orchard bus trip in reverse, so OneMap's order isn't already the sorted one:
    option 1 walks 275 m (43e, 70, 14), options 2 and 3 walk 246 m (62, 683) and (85, 683)."""
    body = onemap_fixture("route_pt_bus_only.json")
    body["plan"]["itineraries"].reverse()
    respx.get(ROUTE).mock(return_value=httpx.Response(200, json=body))


def mock_loads(by_service: dict):
    """Next-bus load per bus service number (everything else is SEA)."""
    def answer(request):
        wanted = request.url.params.get("ServiceNo")
        return httpx.Response(200, json={"Services": [{"ServiceNo": wanted, "Operator": "SBST", "NextBus": bus(
            "2026-10-22T08:05:00+08:00", load=by_service.get(wanted, "SEA"))}]})
    mock_lta()
    respx.get(f"{LTA}/v3/BusArrival").mock(side_effect=answer)


def sorted_by(**kw):
    result = make().plan(*PUNGGOL_TO_ORCHARD, NOW, mode="bus", **kw)
    return result, [o["steps"][1]["line"] for o in result["options"]]            # the first bus of each option


@respx.mock
def test_options_can_be_sorted_by_walking_crowding_or_time():
    ordered_punggol_buses()
    mock_loads({"85": "LSD", "62": "SEA", "43e": "SDA"})
    result, first_buses = sorted_by(sort_by="time")
    assert first_buses == ["43e", "62", "85"] and result["sortedBy"] == "time"      # OneMap's (reversed) order
    result, first_buses = sorted_by(sort_by="walking")
    assert first_buses == ["62", "85", "43e"]                   # 246 m, 246 m (tie keeps OneMap's order), 275 m
    result, first_buses = sorted_by(sort_by="crowding")
    assert first_buses == ["62", "43e", "85"]                   # low, moderate, high
    assert [o["summary"]["crowd"] for o in result["options"]] == ["low", "moderate", "high"]
    assert [o["option"] for o in result["options"]] == [1, 2, 3] and result["sortedBy"] == "crowding"


@respx.mock
def test_bus_step_says_where_the_bus_is_going_and_how_often():
    mock_onemap("route_pt_bus_only_short.json")
    leg = first_leg("route_pt_bus_only_short.json", "BUS")
    board, alight = leg["from"]["stopCode"], leg["to"]["stopCode"]
    mock_lta(bus_routes=[{"ServiceNo": leg["route"], "Direction": 1, "StopSequence": i, "BusStopCode": c}
                         for i, c in enumerate([board, "00001", alight, "00002"])],
             bus_services=[{"ServiceNo": leg["route"], "Direction": 1, "OriginCode": board, "DestinationCode": "00002",
                            "AM_Offpeak_Freq": "06-09"}])
    respx.get(f"{LTA}/BusStops").mock(return_value=httpx.Response(200, json={"value": [
        {"BusStopCode": "00002", "Description": "Punggol Temp Int", "RoadName": "x", "Latitude": 1.4, "Longitude": 103.9}]}))
    bus_step = next(s for s in plan()[0]["steps"] if s["mode"] == "bus")
    assert (bus_step["towards"], bus_step["every"]) == ("Punggol Temp Int", [6, 9])
    assert f"Raise your hand for bus {leg['route']} to Punggol Temp Interchange." in bus_step["easyRead"]
    # How often: said when it fits; with this route's long stop names it is the first sentence left out.
    text, every = bus_step["easyRead"], " It comes every 6 to 9 minutes."
    assert len(text) <= 300 and (every.strip() in text or len(text) + len(every) > 300)
