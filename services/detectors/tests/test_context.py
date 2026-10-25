import json
from pathlib import Path

import fakeredis
import pytest

from detectors.context import load_context
from detectors.models import CheckIn, DetectorRequest

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"
TRIP = "T-001"


def example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


@pytest.fixture
def request_():
    return DetectorRequest.model_validate(example("detector-request.json"))


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def seed_route(redis):
    route = example("route.json")
    redis.set(f"trip:{TRIP}:route", json.dumps({"steps": route["steps"], "path": route["path"]}))
    return route


def seed_context(redis, **overrides):
    values = {"travellerId": "U-042", "stepIndex": 1, "routeVersion": 3, "status": "active"}
    redis.hset(f"trip:{TRIP}:context", mapping={**values, **overrides})


def push_recent(redis, request, seqs):
    """Push check-ins oldest first, so the list ends up newest first (LPUSH)."""
    for seq in seqs:
        c = request.checkin.model_copy(update={"seq": seq})
        redis.lpush(f"trip:{TRIP}:recent", c.model_dump_json(by_alias=True))


def test_full_data(redis, request_):
    route = seed_route(redis)
    seed_context(redis)
    seq = request_.checkin.seq
    push_recent(redis, request_, [seq - 3, seq - 2, seq - 1])

    ctx = load_context(redis, request_)

    assert ctx.steps == route["steps"]
    assert ctx.path == [tuple(p) for p in route["path"]]
    assert ctx.step_index == 1
    assert ctx.route_version == 3
    assert ctx.status == "active"
    assert not ctx.route_version_mismatch
    # oldest -> newest, with the request's own check-in last
    assert [c.seq for c in ctx.recent] == [seq - 3, seq - 2, seq - 1, seq]


def test_missing_route(redis, request_):
    seed_context(redis)
    ctx = load_context(redis, request_)
    assert ctx.steps is None and ctx.path is None
    assert ctx.step_index == 1


def test_empty_redis_falls_back_to_request(redis, request_):
    ctx = load_context(redis, request_)
    assert ctx.steps is None and ctx.path is None
    assert ctx.route_version is None and ctx.status is None
    assert ctx.step_index == request_.checkin.step_index
    assert not ctx.route_version_mismatch
    assert ctx.recent == [request_.checkin]


def test_empty_recent_contains_only_request_checkin(redis, request_):
    seed_route(redis)
    seed_context(redis)
    assert load_context(redis, request_).recent == [request_.checkin]


def test_duplicate_seq_deduped_and_request_wins(redis, request_):
    seq = request_.checkin.seq
    stale = request_.checkin.model_copy(update={"lat": 1.0, "lng": 103.0})
    redis.lpush(f"trip:{TRIP}:recent", stale.model_dump_json(by_alias=True))
    push_recent(redis, request_, [seq - 1, seq - 1])  # duplicate inside Redis too

    ctx = load_context(redis, request_)

    assert [c.seq for c in ctx.recent] == [seq - 1, seq]
    assert ctx.recent[-1].lat == request_.checkin.lat


def test_malformed_recent_entries_skipped(redis, request_):
    seq = request_.checkin.seq
    push_recent(redis, request_, [seq - 2])
    redis.lpush(f"trip:{TRIP}:recent", "not json", '{"seq": 1}')
    push_recent(redis, request_, [seq - 1])

    ctx = load_context(redis, request_)

    assert [c.seq for c in ctx.recent] == [seq - 2, seq - 1, seq]


@pytest.mark.parametrize("raw", ["not json", "[]", '{"steps": []}', '{"steps": [], "path": [[1]]}'])
def test_malformed_route_treated_as_missing(redis, request_, raw):
    redis.set(f"trip:{TRIP}:route", raw)
    ctx = load_context(redis, request_)
    assert ctx.steps is None and ctx.path is None


def test_route_version_mismatch_reported_not_raised(redis, request_):
    seed_route(redis)
    seed_context(redis, routeVersion=4)  # check-in says 3
    ctx = load_context(redis, request_)
    assert ctx.route_version == 4
    assert ctx.route_version_mismatch


def test_works_with_bytes_responses(request_):
    raw = fakeredis.FakeRedis()  # decode_responses=False
    seed_route(raw)
    seed_context(raw)
    push_recent(raw, request_, [request_.checkin.seq - 1])
    ctx = load_context(raw, request_)
    assert ctx.status == "active" and ctx.step_index == 1
    assert len(ctx.recent) == 2


def test_does_not_write(redis, request_):
    seed_route(redis)
    seed_context(redis)
    before = {k: redis.type(k) for k in redis.keys("*")}
    load_context(redis, request_)
    assert {k: redis.type(k) for k in redis.keys("*")} == before
