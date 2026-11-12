import time

import fakeredis

from tools.simulator.probe import BUDGET_MS, Probe, Second, second_line


def test_second_counts_votes_abstains_errors_and_slow_answers():
    s = Second()
    s.add("normal", 8)
    s.add("problem", 9)
    s.add("abstain", 7)
    s.add("normal", BUDGET_MS + 1)  # answered but too late for the coordinator
    s.add("error", 2000)
    line = second_line(3, {"http://a": s, "http://b": Second()})
    assert line["t"] == 3 and line["sent"] == 5 and line["byDetector"]["http://a"]["sent"] == 5
    assert (line["inBudget"], line["votes"], line["abstains"], line["errors"]) == (3, 2, 1, 1)
    assert line["p95Ms"] is not None


def test_empty_second_has_no_latency():
    assert Second().counts() == {"sent": 0, "inBudget": 0, "votes": 0, "abstains": 0, "errors": 0, "p95Ms": None}


def test_probe_seeds_a_trip_and_counts_every_detector():
    r = fakeredis.FakeRedis(decode_responses=True)
    seen = []

    # one detector answers and the other is down
    def post(url, body, timeout):
        seen.append(body["checkin"]["tripId"])
        if url == "http://down":
            raise ConnectionRefusedError()
        return {"verdict": "normal"}

    lines = []
    probe = Probe(r, ["http://up", "http://down"], rate=20, post=post)
    probe.run(1.5, lines.append)
    trip = seen[0]
    assert r.exists(f"trip:{trip}:route") and r.llen(f"trip:{trip}:recent") == 10
    sent = sum(line["sent"] for line in lines)
    assert sent >= 50
    assert sum(line["votes"] for line in lines) == sum(line["errors"] for line in lines) == sent // 2
    assert all(line["byDetector"]["http://down"]["votes"] == 0 for line in lines)


def test_probe_keeps_going_when_redis_is_down():
    class DownRedis:
        def __getattr__(self, name):
            raise ConnectionError("redis down")

    lines = []
    Probe(DownRedis(), ["http://up"], rate=10, post=lambda u, b, t: {"verdict": "abstain"}).run(1.2, lines.append)
    assert sum(line["abstains"] for line in lines) >= 10


def test_a_hanging_redis_does_not_slow_the_calls():
    class SlowRedis:
        def __getattr__(self, name):
            def hang(*args, **kwargs):
                time.sleep(1.0)
                raise ConnectionError("redis timed out")
            return hang

    lines = []
    Probe(SlowRedis(), ["http://up"], rate=20, post=lambda u, b, t: {"verdict": "abstain"}).run(1.5, lines.append)
    assert sum(line["sent"] for line in lines) >= 25
