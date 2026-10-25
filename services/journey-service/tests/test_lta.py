"""LTA DataMall: bus arrival, train alerts, and the cache falling back when Redis is down."""
import httpx
import pytest
import respx

from helpers import EMPTY_BUS, LTA, LTA_NOW, bus, make


@respx.mock
def test_bus_arrival_normalises_and_skips_blank_buses():
    route = respx.get(f"{LTA}/v3/BusArrival").mock(return_value=httpx.Response(200, json={
        "BusStopCode": "83139",
        "Services": [
            {"ServiceNo": "15", "Operator": "GAS", "NextBus": bus("2024-08-14T16:41:48+08:00"),
             "NextBus2": bus("2024-08-14T16:49:22+08:00"), "NextBus3": EMPTY_BUS},
            {"ServiceNo": "150", "Operator": "SBST",
             "NextBus": bus("2024-08-14T16:55:22+08:00", monitored=0, lat="0.0", lng="0.0"),
             "NextBus2": EMPTY_BUS, "NextBus3": EMPTY_BUS},
        ],
    }))
    out = make(clock=LTA_NOW).bus_arrival("83139")

    assert route.calls.last.request.headers["AccountKey"] == "lta"
    assert route.calls.last.request.url.params["BusStopCode"] == "83139"
    s15, s150 = out["services"]
    assert [b["minutes"] for b in s15["buses"]] == [1, 9]
    assert s15["buses"][0]["display"] == "1 min"
    assert s15["buses"][0]["wheelchairAccessible"] is True
    assert s15["buses"][0]["monitored"] is True
    assert s150["buses"][0]["monitored"] is False
    assert s150["buses"][0]["lat"] is None          # 0.0/0.0 means no GPS fix
    assert len(s150["buses"]) == 1


@respx.mock
def test_train_alerts_disruption():
    respx.get(f"{LTA}/TrainServiceAlerts").mock(return_value=httpx.Response(200, json={"value": {
        "Status": 2,
        "AffectedSegments": [{"Line": "NEL", "Direction": "HarbourFront", "Stations": "NE9,NE8,NE7,NE6",
                              "FreePublicBus": "", "FreeMRTShuttle": "", "MRTShuttleDirection": ""}],
        "Message": [{"Content": "1710hrs: NEL - No train service ...", "CreatedDate": "2017-12-01 17:54:21"}],
    }}))
    out = make().train_alerts()
    assert out["status"] == 2
    assert out["segments"][0] == {"line": "NEL", "direction": "HarbourFront",
                                  "stations": ["NE9", "NE8", "NE7", "NE6"],
                                  "freePublicBus": None, "freeMrtShuttle": None}
    assert out["messages"][0]["createdDate"] == "2017-12-01 17:54:21"


@respx.mock
def test_redis_failure_is_a_cache_miss_not_an_error():
    class BrokenRedis:
        def get(self, *a, **k):
            raise ConnectionError("down")

        def set(self, *a, **k):
            raise ConnectionError("down")

    route = respx.get(f"{LTA}/TrainServiceAlerts").mock(return_value=httpx.Response(200, json={
        "value": {"Status": 1, "AffectedSegments": [], "Message": []}}))
    client = make(redis=BrokenRedis())
    assert client.train_alerts()["status"] == 1
    client.train_alerts()
    assert route.call_count == 2            # nothing cached, but nothing failed either


@pytest.mark.parametrize("load,crowd", [("SEA", "low"), ("SDA", "moderate"), ("LSD", "high"), ("", None)])
@respx.mock
def test_a_buses_load_becomes_a_crowd_level(load, crowd):
    respx.get(f"{LTA}/v3/BusArrival").mock(return_value=httpx.Response(200, json={"BusStopCode": "1", "Services": [
        {"ServiceNo": "15", "Operator": "GAS", "NextBus": bus("2024-08-14T16:41:48+08:00", load=load),
         "NextBus2": EMPTY_BUS, "NextBus3": EMPTY_BUS}]}))
    assert make(clock=LTA_NOW).bus_arrival("1")["services"][0]["buses"][0]["crowd"] == crowd
