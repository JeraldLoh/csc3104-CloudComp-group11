from datetime import datetime, timedelta, timezone

import pytest

from detectors.models import CheckIn
from tools.simulator.live import Endpoints, LiveRun, ServiceError
from tools.simulator.scenarios import generate

NOW = datetime(2026, 11, 1, 8, 0, 0, tzinfo=timezone.utc)


# answers the three services would give and records every request
class FakeServices:
    def __init__(self, travellers=(), profiles_status=200):
        self.travellers = list(travellers)
        self.profiles_status = profiles_status
        self.requests = []
        self.checkins = []

    def __call__(self, method, url, body):
        self.requests.append((method, url))
        path = url.split(":", 2)[2].split("/", 1)[1]
        if path.startswith("api/travellers"):
            if self.profiles_status != 200:
                raise ServiceError(url, self.profiles_status, "needs a login")
            if method == "POST":
                return {"travellerId": "U-NEW"}
            return self.travellers
        if path == "api/routes":
            return {"routeId": "R-1", "version": 1}
        if path == "api/trips":
            return {"tripId": "T-1"}
        if path == "api/checkins":
            self.checkins.append(body)
            return {"accepted": 1}
        if path.startswith("api/incidents"):
            return [{"incidentId": "U-1#T-1#off_route#0"}, {"incidentId": "U-1#T-OLD#off_route#0"}]
        return {}


# a run whose clock moves only when it sleeps
def run_with(services):
    clock = [NOW]
    run = LiveRun(Endpoints(), "G-9", services, sleep=lambda s: clock.__setitem__(0, clock[0] + timedelta(seconds=s)),
                  now=lambda: clock[0])
    return run, clock


def test_reuses_the_caregivers_first_traveller():
    run, _ = run_with(FakeServices(travellers=[{"travellerId": "U-1"}]))
    assert run.choose_traveller() == "U-1"


def test_creates_a_traveller_when_the_caregiver_has_none():
    services = FakeServices()
    run, _ = run_with(services)
    assert run.choose_traveller() == "U-NEW"
    assert ("POST", "http://localhost:8005/api/travellers") in services.requests


def test_given_traveller_skips_the_alert_service():
    services = FakeServices(profiles_status=401)
    run, _ = run_with(services)
    assert run.choose_traveller("U-7") == "U-7" and services.requests == []


def test_login_off_asks_for_a_traveller():
    run, _ = run_with(FakeServices(profiles_status=401))
    with pytest.raises(SystemExit, match="--traveller"):
        run.choose_traveller()


def test_starts_an_approved_trip_for_the_traveller():
    services = FakeServices(travellers=[{"travellerId": "U-1"}])
    run, _ = run_with(services)
    run.choose_traveller()
    assert run.start_trip() == 1 and run.trip_id == "T-1"
    assert [u for m, u in services.requests if m == "POST"] == [
        "http://localhost:8006/api/routes",
        "http://localhost:8006/api/routes/R-1/approve",
        "http://localhost:8006/api/trips",
        "http://localhost:8006/api/trips/T-1/start"
    ]


@pytest.mark.parametrize("browser", [False, True])
def test_checkins_keep_the_scenario_spacing_and_never_run_ahead_of_the_clock(browser):
    services = FakeServices(travellers=[{"travellerId": "U-1"}])
    run, clock = run_with(services)
    run.choose_traveller()
    run.start_trip()
    stamps = []
    run.send("long_dwell", 1, 0.5, seed=3, browser=browser, on_sent=lambda c: stamps.append(clock[0]))
    sent = [CheckIn.model_validate(c) for c in services.checkins]
    expected = generate("long_dwell", 3, browser=browser)
    assert len(sent) == len(expected)
    assert all(c.trip_id == "T-1" and c.traveller_id == "U-1" and c.route_version == 1 for c in sent)
    gaps = [(b.timestamp - a.timestamp).total_seconds() for a, b in zip(sent, sent[1:])]
    want = [(datetime.fromisoformat(b["timestamp"].replace("Z", "+00:00"))
             - datetime.fromisoformat(a["timestamp"].replace("Z", "+00:00"))).total_seconds()
            for a, b in zip(expected, expected[1:])]
    assert gaps == want
    assert all(c.timestamp <= at for c, at in zip(sent, stamps))  # never in the future
    assert sent[-1].timestamp == stamps[-1]


def test_lists_only_this_trips_incidents():
    run, _ = run_with(FakeServices(travellers=[{"travellerId": "U-1"}]))
    run.choose_traveller()
    run.start_trip()
    assert [i["incidentId"] for i in run.incidents()] == ["U-1#T-1#off_route#0"]
