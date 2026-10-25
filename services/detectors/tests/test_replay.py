import csv

import fakeredis
import pytest
from fastapi.testclient import TestClient

from detectors.app import create_app
from detectors.config import Settings
from tools.simulator import replay

URLS = ["http://rc", "http://hs", "http://dw"]
IDS = ["route-corridor", "heading-speed", "dwell-time"]


@pytest.fixture
def stack():
    """Real detector apps sharing one fakeredis, called in-process instead of over HTTP."""
    r = fakeredis.FakeRedis(decode_responses=True)
    clients = {u: TestClient(create_app(Settings(detector_id=i), r)) for u, i in zip(URLS, IDS)}
    for c in clients.values():
        c.__enter__()
    yield r, lambda url, body: clients[url].post("/internal/detect", json=body).json()
    for c in clients.values():
        c.__exit__(None, None, None)


def run(stack, scenario):
    r, post = stack
    return r, replay.replay_scenario(scenario, r, URLS, post=post)


def test_replay_seeds_redis_like_the_checkin_service(stack):
    r, rows = run(stack, "off_route")
    trip = "T-SIM-OFF-ROUTE"
    assert r.llen(f"trip:{trip}:recent") == 10
    newest = r.lindex(f"trip:{trip}:recent", 0)
    assert f'"seq": {rows[-1]["seq"]}' in newest
    assert "scenario" not in newest and "expected" not in newest  # labels never reach Redis
    assert r.hget(f"trip:{trip}:context", "routeVersion") == "3"
    assert r.exists(f"trip:{trip}:route")


def test_off_route_is_flagged_by_its_detector_only(stack):
    _, rows = run(stack, "off_route")
    assert [v["detectorId"] for v in rows[0]["votes"]] == IDS
    summary = replay.summarize({"off_route": rows})
    assert summary["route-corridor"]["true_positives"] > 0
    assert summary["route-corridor"]["delays"]["off_route"] is not None
    assert all(summary[i]["false_alarms"] == 0 for i in IDS)


def test_normal_has_no_false_alarms(stack):
    summary = replay.summarize({"normal": run(stack, "normal")[1]})
    assert all(d["false_alarms"] == 0 and d["true_positives"] == 0 for d in summary.values())


def test_dead_detector_is_an_error_not_a_crash(stack):
    r, post = stack

    def flaky(url, body):
        if url == URLS[1]:
            raise ConnectionError("down")
        return post(url, body)

    rows = replay.replay_scenario("normal", r, URLS, post=flaky)
    assert {row["votes"][1]["verdict"] for row in rows} == {"error"}
    assert replay.summarize({"normal": rows})[URLS[1]]["errors"] == len(rows)


def test_summarize_counts_and_delay():
    def row(expected, *verdicts):
        return {"seq": 0, "expected": expected, "votes": [
            {"detectorId": "route-corridor", "verdict": v, "problemType": t, "latencyMs": 1.0} for v, t in verdicts]}

    rows = [
        row("normal", ("normal", None)),
        row("off_route", ("normal", None)),
        row("off_route", ("normal", None)),
        row("off_route", ("problem", "off_route")),
        row("normal", ("problem", "off_route")),  # fires after the episode ended: a false alarm
        row("normal", ("abstain", None)),
    ]
    d = replay.summarize({"off_route": rows})["route-corridor"]
    assert (d["true_positives"], d["false_alarms"], d["abstains"]) == (1, 1, 1)
    assert d["delays"] == {"off_route": 2}  # labelled at row 1, first fired at row 3


def test_csv_written(stack, tmp_path):
    _, rows = run(stack, "normal")
    path = replay.write_csv(rows, "normal", tmp_path)
    with path.open() as f:
        data = list(csv.reader(f))
    assert data[0][:2] == ["seq", "expected"] and len(data) == len(rows) + 1
    assert path.parent == tmp_path and path.name.startswith("normal-")
