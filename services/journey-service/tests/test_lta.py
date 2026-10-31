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


# ----- where a bus is going and how often it comes (BusRoutes / BusServices) -----

ROUTES = [{"ServiceNo": "136", "Direction": d, "StopSequence": i, "BusStopCode": c}
          for d, codes in ((1, ["65009", "65221", "65259"]), (2, ["65259", "65229", "65009"])) for i, c in enumerate(codes)]
SERVICES = [{"ServiceNo": "136", "Direction": 1, "OriginCode": "65009", "DestinationCode": "65259", "AM_Offpeak_Freq": "08-12"},
            {"ServiceNo": "136", "Direction": 2, "OriginCode": "65259", "DestinationCode": "65009", "AM_Offpeak_Freq": "-"},
            {"ServiceNo": "88", "Direction": 1, "OriginCode": "65009", "DestinationCode": "65009", "AM_Offpeak_Freq": "10-10"}]
STOPS = [{"BusStopCode": "65259", "Description": "Punggol Int", "RoadName": "Punggol Ctrl", "Latitude": 1.4, "Longitude": 103.9},
         {"BusStopCode": "65009", "Description": "Blk 119D", "RoadName": "Rivervale Dr", "Latitude": 1.39, "Longitude": 103.9}]


def bus_lists(routes=ROUTES):
    routes = respx.get(f"{LTA}/BusRoutes").mock(return_value=httpx.Response(200, json={"value": routes}))
    respx.get(f"{LTA}/BusServices").mock(return_value=httpx.Response(200, json={"value": SERVICES}))
    respx.get(f"{LTA}/BusStops").mock(return_value=httpx.Response(200, json={"value": STOPS}))
    return routes


@respx.mock
def test_bus_service_finds_the_direction_that_stops_at_both_in_order():
    routes = bus_lists()
    client = make()
    assert client.lta.bus_service("136", "65009", "65221") == {"towards": "Punggol Int", "every": [8, 12]}
    assert client.lta.bus_service("136", "65259", "65009") == {"towards": "Blk 119D", "every": None}  # no daytime figure
    assert client.lta.bus_service("136", "65221", "65009") is None          # wrong way round on both directions
    assert client.lta.bus_service("999", "65009", "65221") is None
    assert routes.call_count == 1                                           # the lists are kept


@respx.mock
def test_bus_service_loop_has_no_destination():
    bus_lists([{"ServiceNo": "88", "Direction": 1, "StopSequence": i, "BusStopCode": c}
               for i, c in enumerate(["65009", "65259"])])
    # A loop: every bus 88 at that pole goes the right way, so no destination is named.
    assert make().lta.bus_service("88", "65009", "65259") == {"towards": None, "every": [10, 10]}


@respx.mock
def test_bus_service_unknown_when_lta_is_down_and_not_asked_again_at_once():
    down = respx.get(f"{LTA}/BusRoutes").mock(return_value=httpx.Response(503))
    respx.get(f"{LTA}/BusServices").mock(return_value=httpx.Response(503))
    lta = make().lta
    assert lta.bus_service("136", "65009", "65221") is None
    assert lta.bus_service("136", "65009", "65221") is None
    assert down.call_count == 1                              # the failure is remembered for BUS_RETRY_S


def test_bus_service_does_not_wait_for_a_load_in_progress():
    lta = make().lta
    lta._bus_lock.acquire()                                  # the startup prefetch is loading the lists
    try:
        assert lta.bus_service("136", "65009", "65221") is None
    finally:
        lta._bus_lock.release()


def test_every():
    from app.lta import every
    assert [every(x) for x in ("08-12", "10-10", "-", "", None, "00-00", "12-08")] == [[8, 12], [10, 10], None, None, None, None, None]
