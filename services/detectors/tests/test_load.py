import json
import threading

import fakeredis
import pytest

from tools.simulator import load


# post that records every request and fail_url always times out
def fake_detectors(fail_url=None):
    seen = []
    lock = threading.Lock()

    def post(url, body):
        with lock:
            seen.append((url, body))
        if url == fail_url:
            raise TimeoutError("too slow")
        return {"verdict": "normal"}

    return seen, post


def run(travellers=10, interval=0.05, duration=0.5, urls=("http://a", "http://b"), **kw):
    r = fakeredis.FakeRedis(decode_responses=True)
    seen, post = fake_detectors(kw.pop("fail_url", None))
    stats = load.run_load(r, list(urls), travellers, interval, duration, post=post, report_every_s=0.1, run_id="T", **kw)
    return r, seen, stats


def test_every_checkin_goes_to_every_detector():
    _, seen, stats = run()
    by_url = {}
    for url, body in seen:
        by_url.setdefault(url, []).append(body["requestId"])
    assert set(by_url) == {"http://a", "http://b"}
    assert sorted(by_url["http://a"]) == sorted(by_url["http://b"])
    assert stats.total.sent == len(seen)


def test_rate_is_travellers_over_interval():
    # 10 travellers every 0.05 s for 0.5 s is about 100 check-ins
    _, seen, _ = run(urls=("http://a",))
    assert 80 <= len(seen) <= 110


def test_each_traveller_has_its_own_trip_and_ordered_seqs():
    _, seen, _ = run(urls=("http://a",))
    seqs = {}
    for _, body in seen:
        c = body["checkin"]
        assert c["tripId"].startswith("LOAD-T-")
        assert body["requestId"] == f"{c['tripId']}#{c['seq']}"
        seqs.setdefault((c["travellerId"], c["tripId"]), []).append(c["seq"])
    assert len({t for t, _ in seqs}) == 10
    assert all(s == sorted(s) for s in seqs.values())


def test_redis_is_written_like_the_checkin_service_and_expires():
    r, seen, _ = run(urls=("http://a",))
    trip = seen[-1][1]["checkin"]["tripId"]
    recent = r.lrange(f"trip:{trip}:recent", 0, -1)
    assert 1 <= len(recent) <= 10
    newest = json.loads(recent[0])
    assert "expected" not in newest and "scenario" not in newest  # labels never reach Redis
    assert r.exists(f"trip:{trip}:route")
    assert r.hget(f"trip:{trip}:context", "routeVersion") == "3"
    for key in (f"trip:{trip}:recent", f"trip:{trip}:route", f"trip:{trip}:context"):
        assert 0 < r.ttl(key) <= load.KEY_TTL_S


def test_a_failing_detector_is_counted_not_fatal():
    _, _, stats = run(fail_url="http://b")
    assert stats.per_detector["http://b"].verdicts == {"error": stats.per_detector["http://b"].sent}
    assert stats.per_detector["http://a"].verdicts.get("error", 0) == 0


def test_a_finished_trip_starts_a_new_one():
    t = load.Traveller(0, "T", "wrong_direction", seed=1, browser=True)
    first, new = t.next_checkin()
    assert new and first["seq"] == 1
    trip = first["tripId"]
    while True:
        c, new = t.next_checkin()
        if new:
            break
    assert c["tripId"] != trip and c["seq"] == 1


def test_scenarios_are_assigned_round_robin():
    _, seen, _ = run(travellers=4, scenarios=["normal", "poor_gps"])
    poor = {b["checkin"]["travellerId"] for _, b in seen if b["checkin"]["accuracyM"] > 50}
    assert poor == {"U-LOAD-0001", "U-LOAD-0003"}


@pytest.mark.parametrize("kwargs", [{"travellers": 0}, {"interval": 0}, {"duration": 0}, {"urls": ()}])
def test_bad_arguments_are_rejected(kwargs):
    with pytest.raises(ValueError):
        run(**kwargs)


def test_unknown_scenario_is_rejected():
    with pytest.raises(ValueError):
        run(scenarios=["teleport"])


def test_percentile_is_nearest_rank():
    values = list(range(1, 11))
    assert load.percentile(values, 50) == 5
    assert load.percentile(values, 95) == 10
    assert load.percentile([], 50) is None
