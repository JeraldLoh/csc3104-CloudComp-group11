"""Disruption checker (what the Lambda runs)."""
import json
from pathlib import Path

import fakeredis
import httpx
import respx

from app.disruption import DisruptionChecker
from app.lta import LtaClient
from helpers import DISRUPTION_NOW, LTA, NEL_ALERT, SETTINGS


def stop(code, name):
    return {"code": code, "name": name}


def trip_route(traveller_id="U-042"):
    return {
        "routeId": "R-1", "travellerId": traveller_id,
        "steps": [
            {"stepIndex": 0, "mode": "walk", "from": stop(None, None), "to": stop("NE17", "Punggol MRT Station")},
            {"stepIndex": 1, "mode": "train", "line": "NE", "from": stop("NE17", "Punggol MRT Station"),
             "to": stop("NE6", "Dhoby Ghaut MRT Station"),
             "stops": [stop("NE17", "Punggol MRT Station"), stop("NE12", "Serangoon MRT Station"),
                       stop("NE11", "Woodleigh MRT Station"), stop("NE10", "Potong Pasir MRT Station"),
                       stop("NE6", "Dhoby Ghaut MRT Station")]},
            {"stepIndex": 2, "mode": "walk", "from": stop("NE6", "Dhoby Ghaut MRT Station"), "to": stop(None, None)},
        ],
    }


def checker_over(redis, settings=SETTINGS) -> DisruptionChecker:
    """The disruption checker the Lambda runs: just Redis, the LTA client and the settings (no API code)."""
    lta = LtaClient(settings, redis_client=redis, sleep=lambda _: None, clock=lambda: DISRUPTION_NOW)
    return DisruptionChecker(redis, lta, settings, clock=lambda: DISRUPTION_NOW)


class Harness:
    """A disruption checker over fake Redis, collecting the messages it would put on the queue."""

    def __init__(self, alert=NEL_ALERT, settings=SETTINGS):
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.checker = checker_over(self.redis, settings)
        self.sent: list[dict] = []
        self.fail_next = False
        self.alert = alert

    def send(self, event):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("SQS down")
        self.sent.append(event)

    def add_trip(self, trip_id="T-001", step=0, route_json=None, traveller="U-042"):
        self.redis.sadd("active-trips", trip_id)
        if route_json is not False:
            self.redis.set(f"trip:{trip_id}:route", json.dumps(route_json or trip_route()))
        ctx = {"stepIndex": step}
        if traveller:
            ctx["travellerId"] = traveller
        self.redis.hset(f"trip:{trip_id}:context", mapping=ctx)

    def run(self, send=None):
        with respx.mock:
            respx.get(f"{LTA}/TrainServiceAlerts").mock(return_value=httpx.Response(200, json=self.alert))
            return self.checker.check(send or self.send)


def test_disruption_on_upcoming_train_step_is_sent_in_contract_format():
    h = Harness()
    h.add_trip(step=0)
    summary = h.run()
    assert summary["sent"] == 1 and summary["activeTrips"] == 1
    assert h.sent == [{
        "disruptionId": "D-NEL-20261021-0133",
        "source": "lta-datamall",
        "tripId": "T-001",
        "travellerId": "U-042",
        "affectedStepIndex": 1,
        "mode": "train",
        "line": "NEL",
        "affectedStations": ["Serangoon MRT Station", "Woodleigh MRT Station", "Potong Pasir MRT Station"],
        "message": "NEL: no train service between Serangoon and Potong Pasir",
        "detectedAt": "2026-10-21T02:05:12Z",
    }]


def test_same_disruption_is_not_sent_twice():
    h = Harness()
    h.add_trip()
    assert h.run()["sent"] == 1
    second = h.run()
    assert second["sent"] == 0 and second["duplicates"] == 1
    assert len(h.sent) == 1


def test_minor_delays_do_not_alert():
    h = Harness(alert={"value": {**NEL_ALERT["value"], "Status": 1}})
    h.add_trip()
    assert h.run()["sent"] == 0 and h.sent == []


def test_trip_on_a_different_line_is_not_alerted():
    r = trip_route()
    r["steps"][1]["stops"] = [stop("EW1", "Pasir Ris"), stop("EW2", "Tampines")]
    r["steps"][1]["from"], r["steps"][1]["to"] = stop("EW1", "Pasir Ris"), stop("EW2", "Tampines")
    h = Harness()
    h.add_trip(route_json=r)
    assert h.run()["sent"] == 0


def test_the_lambda_does_not_import_the_api_code():
    """The Lambda package must not need FastAPI, the login code (PyJWT), route stores or OneMap.
    (`cryptography` isn't checked: the `redis` package itself probes for it if it happens to be installed.)"""
    import subprocess
    import sys
    probe = ("import sys; import app.lambda_handler; "
             "print(','.join(sorted(m.split('.')[1] for m in sys.modules if m.startswith('app.') and m.count('.') == 1)));"
             "print(','.join(p for p in ('fastapi', 'starlette', 'pydantic', 'jwt') if p in sys.modules))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).resolve().parents[1]).stdout.splitlines()
    assert set(out[0].split(",")) == {"lambda_handler", "aws_resources", "config", "cache", "lta", "disruption"}
    assert out[1] == ""                                   # none of the API's third-party packages were loaded
