"""Demo control against an in-memory Redis, a fake SQS and fake /control endpoints."""
import asyncio
import json
from pathlib import Path

import fakeredis
import httpx
import pytest

from app.main import DemoControl, Settings, create_app
from app.scenarios import OFF, RIDE, SCENARIOS, WAIT, WALK, build_checkin

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
ROUTE_FILE = str(CONTRACTS / "route.json")
CHECKIN_FIELDS = set(json.loads((CONTRACTS / "checkin.json").read_text()))


class FakeSqs:
    def __init__(self):
        self.sent = []

    def get_queue_url(self, QueueName):
        return {"QueueUrl": f"http://sqs/{QueueName}"}

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append(json.loads(MessageBody))


class Controls:
    """The fakes' /control endpoints: records calls; `real` hosts answer 404 like real detectors."""

    def __init__(self, real=()):
        self.calls = []
        self.real = set(real)

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.url.host, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.host in self.real:
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, json={"ok": True})


def settings(**kw) -> Settings:
    return Settings(**{"route_file": ROUTE_FILE, "interval_s": 0, "trip_id": "T-001", "traveller_id": "U-042", **kw})


class Docker:
    """The Docker Engine API: this project's containers, and stop/start calls."""

    def __init__(self, states=None):
        self.states = dict(states or {"alert-service": "running", "detector-route": "running", "redis": "running"})
        self.calls = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/containers/json":
            assert "careroute-analysis" in req.url.params["filters"]
            return httpx.Response(200, json=[
                {"Id": f"id-{name}", "State": state, "Labels": {"com.docker.compose.service": name}}
                for name, state in self.states.items()])
        name, action = req.url.path.split("/")[2].removeprefix("id-"), req.url.path.split("/")[3]
        self.calls.append((name, action))
        new = "exited" if action == "stop" else "running"
        if self.states[name] == new:
            return httpx.Response(304)
        self.states[name] = new
        return httpx.Response(204)


class Dynamo:
    """DynamoDB scan/delete, two pages."""

    def __init__(self, ids):
        self.items = list(ids)

    def scan(self, TableName, ProjectionExpression, ExclusiveStartKey=None):
        assert TableName == "careroute-incidents"
        start = int(ExclusiveStartKey["n"]["N"]) if ExclusiveStartKey else 0
        page = {"Items": [{"incidentId": {"S": i}} for i in self.items[start:start + 2]]}
        if start + 2 < len(self.items):
            page["LastEvaluatedKey"] = {"n": {"N": str(start + 2)}}
        return page

    def delete_item(self, TableName, Key):
        self.items[self.items.index(Key["incidentId"]["S"])] = None


async def make(controls=None, docker=None, dynamodb=None):
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    sqs = FakeSqs()
    http = httpx.AsyncClient(transport=httpx.MockTransport((controls or Controls()).handler))
    d = httpx.AsyncClient(transport=httpx.MockTransport(docker.handler), base_url="http://docker") if docker else None
    return DemoControl(redis, sqs, http, settings(), docker=d, dynamodb=dynamodb), redis, sqs, http


async def finish(control: DemoControl):
    while control.running:
        await asyncio.sleep(0.01)


# --- check-ins -----------------------------------------------------------------------------

def test_checkin_has_the_contract_fields():
    c = build_checkin("T-001", "U-042", 1, WALK, 3)
    assert set(c) == CHECKIN_FIELDS
    assert c["timestamp"].endswith("Z")


@pytest.mark.parametrize("move, step, speed", [(WALK, 0, 1.3), (WAIT, 1, 0.2), (RIDE, 1, 9.0), (OFF, 0, 1.3)])
def test_moves_match_the_journey_steps(move, step, speed):
    c = build_checkin("T-001", "U-042", 1, move, 3)
    assert (c["stepIndex"], c["speedMps"]) == (step, speed)


def test_send_writes_redis_like_the_checkin_service_then_queues():
    async def go():
        control, redis, sqs, http = await make()
        c = await control.send(WALK)
        recent = [json.loads(x) for x in await redis.lrange("trip:T-001:recent", 0, -1)]
        context = await redis.hgetall("trip:T-001:context")
        await http.aclose()
        return c, recent, context, sqs.sent
    c, recent, context, sent = asyncio.run(go())
    assert sent == [c] and recent == [c]
    assert context == {"travellerId": "U-042", "stepIndex": "0", "routeVersion": "3", "status": "active"}
    assert c["routeVersion"] == 3   # from contracts/examples/route.json


def test_seq_increases_and_recent_keeps_ten():
    async def go():
        control, redis, sqs, http = await make()
        for _ in range(12):
            await control.send(WALK)
        n = await redis.llen("trip:T-001:recent")
        await http.aclose()
        return [m["seq"] for m in sqs.sent], n
    seqs, n = asyncio.run(go())
    assert seqs == list(range(1, 13)) and n == 10


# --- scenarios -----------------------------------------------------------------------------

def test_every_scenario_sends_its_checkins_in_order():
    async def go(name):
        control, redis, sqs, http = await make()
        assert control.run(name) == len(SCENARIOS[name]["moves"])
        await finish(control)
        await http.aclose()
        return sqs.sent
    for name, sc in SCENARIOS.items():
        sent = asyncio.run(go(name))
        assert [(m["stepIndex"], m["speedMps"], m["lat"]) for m in sent] == \
            [(mv.step, mv.speed_mps, mv.lat) for mv in sc["moves"]], name


def test_off_route_scenario_has_three_in_a_row():
    # route-corridor needs 3 consecutive off-route check-ins before it flags.
    assert SCENARIOS["off-route"]["moves"] == [OFF, OFF, OFF]


def test_only_one_scenario_at_a_time():
    async def go():
        control, *_rest, http = await make()
        control.s = settings(interval_s=0.05)
        control.run("journey")
        with pytest.raises(RuntimeError):
            control.run("walk")
        await finish(control)
        control.run("walk")    # free again
        await finish(control)
        await http.aclose()
    asyncio.run(go())


def test_unknown_scenario():
    async def go():
        control, *_rest, http = await make()
        with pytest.raises(KeyError):
            control.run("teleport")
        await http.aclose()
    asyncio.run(go())


# --- duplicate, faults, reset -----------------------------------------------------------------

def test_duplicate_resends_the_same_seq():
    async def go():
        control, redis, sqs, http = await make()
        assert await control.duplicate() is None
        await control.send(OFF)
        await control.duplicate()
        await http.aclose()
        return sqs.sent
    sent = asyncio.run(go())
    assert len(sent) == 2 and sent[0] == sent[1]


def test_fault_reaches_the_fakes_control():
    controls = Controls()
    async def go():
        control, *_rest, http = await make(controls)
        result = await control.fault("alert-service", {"mode": "error"})
        await http.aclose()
        return result
    assert asyncio.run(go())[0] == 200
    assert controls.calls == [("alert-service", "/control", {"mode": "error"})]


def test_fault_on_a_real_detector_says_start_the_fakes():
    async def go():
        control, *_rest, http = await make(Controls(real={"detector-route"}))
        result = await control.fault("detector-route", {"delayMs": 2000})
        await http.aclose()
        return result
    code, body = asyncio.run(go())
    assert code == 409 and body["error"]["code"] == "not_a_fake"


def test_reset_clears_everything_and_reseeds_the_trip():
    controls = Controls()
    async def go():
        control, redis, sqs, http = await make(controls)
        await control.send(OFF)
        await redis.hset("risk:U-042", mapping={"level": "elevated"})
        await control.reset()
        keys = sorted(await redis.keys("*"))
        context = await redis.hgetall("trip:T-001:context")
        await http.aclose()
        return keys, context
    keys, context = asyncio.run(go())
    assert keys == ["trip:T-001:context", "trip:T-001:route"]
    assert context["stepIndex"] == "0" and context["routeVersion"] == "3"
    assert ("alert-service", "/control/reset", None) in controls.calls


def test_reset_clears_the_real_alert_services_incidents():
    dynamo = Dynamo(["a", "b", "c"])
    async def go():
        control, *_rest, http = await make(dynamodb=dynamo)
        await control.reset()
        await http.aclose()
    asyncio.run(go())
    assert dynamo.items == [None, None, None]


def test_stop_and_start_a_real_service():
    docker = Docker()
    async def go():
        control, *_rest, http = await make(docker=docker)
        stopped = await control.switch("alert-service", "stop")
        states = await control.services()
        again = await control.switch("alert-service", "stop")     # already stopped: still fine
        started = await control.switch("alert-service", "start")
        await http.aclose()
        return stopped, states, again, started
    stopped, states, again, started = asyncio.run(go())
    assert stopped == (200, {"target": "alert-service", "state": "exited"})
    assert states == {"alert-service": "exited", "detector-route": "running"}   # only the controllable ones
    assert again[0] == 200 and started == (200, {"target": "alert-service", "state": "running"})
    assert docker.calls == [("alert-service", "stop"), ("alert-service", "stop"), ("alert-service", "start")]


def test_switch_without_docker_or_container():
    async def go():
        bare, *_r, h1 = await make()
        no_docker = await bare.switch("alert-service", "stop")
        control, *_r, h2 = await make(docker=Docker({"redis": "running"}))
        missing = await control.switch("detector-dwell", "stop")
        await h1.aclose(); await h2.aclose()
        return no_docker, missing, await bare.services()
    no_docker, missing, services = asyncio.run(go())
    assert no_docker[0] == 503 and missing[0] == 404 and services == {}


def test_status():
    async def go():
        control, *_rest, http = await make()
        await control.send(WALK)
        s = await control.status()
        await http.aclose()
        return s
    s = asyncio.run(go())
    assert s["lastSeq"] == 1 and s["running"] is None and s["trip"] == "T-001"
    assert [sc["name"] for sc in s["scenarios"]] == list(SCENARIOS)


# --- HTTP -------------------------------------------------------------------------------------

def call(method, path, body=None, controls=None):
    async def go():
        control, redis, sqs, http = await make(controls)
        app = create_app(control=control, s=settings())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo") as c:
            r = await c.request(method, path, json=body)
            await finish(control)
        await http.aclose()
        return r, sqs.sent
    return asyncio.run(go())


def test_page_is_served():
    r, _ = call("GET", "/")
    assert r.status_code == 200 and "Demo control" in r.text
    assert "innerHTML" not in r.text


def test_run_endpoint():
    r, sent = call("POST", "/api/demo/run", {"scenario": "off-route"})
    assert r.status_code == 202 and r.json() == {"scenario": "off-route", "checkins": 3}
    assert len(sent) == 3


@pytest.mark.parametrize("method, path, body, code", [
    ("POST", "/api/demo/run", {"scenario": "teleport"}, 404),
    ("POST", "/api/demo/duplicate", None, 409),
    ("POST", "/api/demo/fault", {"target": "coordinator", "mode": "error"}, 404),
    ("POST", "/api/demo/fault", {"target": "alert-service"}, 422),
    ("POST", "/api/demo/service", {"target": "redis", "action": "stop"}, 404),
    ("POST", "/api/demo/service", {"target": "alert-service", "action": "explode"}, 422),
])
def test_endpoint_errors(method, path, body, code):
    r, _ = call(method, path, body)
    assert r.status_code == code
    assert "error" in r.json()


def test_reset_endpoint():
    r, _ = call("POST", "/api/demo/reset")
    assert r.status_code == 200 and r.json() == {"status": "reset"}
