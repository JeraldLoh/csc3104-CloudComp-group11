"""Demo control panel (local development only, never deployed).

Lets a presenter drive the local stack from a browser instead of the command line:

GET  /                      the panel: buttons on top, the dashboard embedded below
GET  /api/demo/status       scenarios, whether one is running, last seq sent
POST /api/demo/run          {"scenario": "walk"}  sends that scenario's check-ins, one every INTERVAL_S
POST /api/demo/duplicate    sends the last check-in again (same seq: the Coordinator skips it)
POST /api/demo/service      {"target": "alert-service", "action": "stop" | "start"}
                            stops or starts that container (real or fake), through the Docker socket
POST /api/demo/fault        {"target": "detector-route", "delayMs": 2000}   (fake detectors only)
POST /api/demo/reset        clears Redis, re-seeds the trip, clears the Alert Service's incidents

It stands in for the Check-in Service (Member 2) the same way send_checkin.py does: it writes
trip:{id}:recent and :context to Redis (the detectors read them), then puts the check-in on
careroute-checkins.
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import boto3
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .scenarios import SCENARIOS, Move, build_checkin

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("demo-control")

PAGE = Path(__file__).parent / "static" / "demo.html"
# Services the panel may stop or start, or (fakes only) send fault orders to. Compose service names.
FAULT_TARGETS = {"alert-service", "detector-route", "detector-heading", "detector-dwell", "detector-progress"}
SERVICE_LABEL = "com.docker.compose.service"


@dataclass(frozen=True)
class Settings:
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    aws_endpoint_url: str | None = field(default_factory=lambda: os.getenv("AWS_ENDPOINT_URL") or None)
    aws_region: str = field(default_factory=lambda: os.getenv("AWS_DEFAULT_REGION", "ap-southeast-1"))
    queue_name: str = field(default_factory=lambda: os.getenv("CHECKINS_QUEUE", "careroute-checkins"))
    trip_id: str = field(default_factory=lambda: os.getenv("TRIP", "T-001"))
    traveller_id: str = field(default_factory=lambda: os.getenv("TRAVELLER", "U-042"))
    route_file: str = field(default_factory=lambda: os.getenv("ROUTE_FILE", "/tools/route.json"))
    interval_s: float = field(default_factory=lambda: float(os.getenv("INTERVAL_S", "1.5")))
    dashboard_url: str = field(default_factory=lambda: os.getenv("DASHBOARD_URL", "http://localhost:8001/dashboard"))
    # Member 5's caregiver app (Alerts tab), shown beside the dashboard. Started with npm, not compose.
    caregiver_url: str = field(default_factory=lambda: os.getenv("CAREGIVER_URL", "http://localhost:5174/#alerts"))
    # Stop/start buttons: the containers of this compose project, through the Docker socket.
    compose_project: str = field(default_factory=lambda: os.getenv("COMPOSE_PROJECT", "careroute-analysis"))
    docker_socket: str = field(default_factory=lambda: os.getenv("DOCKER_SOCKET", "/var/run/docker.sock"))
    # Reset clears the Alert Service's incidents in local DynamoDB. Unset: skipped.
    dynamodb_endpoint_url: str | None = field(default_factory=lambda: os.getenv("DYNAMODB_ENDPOINT_URL") or None)
    incidents_table: str = field(default_factory=lambda: os.getenv("INCIDENTS_TABLE", "careroute-incidents"))


def seq_key(trip_id: str) -> str:
    return f"demo:seq:{trip_id}"


def last_key(trip_id: str) -> str:
    return f"demo:last:{trip_id}"


class DemoControl:
    def __init__(self, redis, sqs, http: httpx.AsyncClient, s: Settings,
                 docker: httpx.AsyncClient | None = None, dynamodb=None):
        self.redis = redis
        self.sqs = sqs
        self.http = http
        self.s = s
        self.docker = docker        # base_url http://docker, over the Docker socket; None: no buttons
        self.dynamodb = dynamodb    # local DynamoDB of the real Alert Service; None: nothing to clear
        self.running: str | None = None
        self._task: asyncio.Task | None = None
        self._queue_url: str | None = None

    def _route(self) -> dict:
        return json.loads(Path(self.s.route_file).read_text())

    async def _queue(self) -> str:
        if self._queue_url is None:
            self._queue_url = (await asyncio.to_thread(self.sqs.get_queue_url, QueueName=self.s.queue_name))["QueueUrl"]
        return self._queue_url

    async def _put(self, checkin: dict) -> None:
        """Like the Check-in Service: live state in Redis first, then the queue."""
        t = checkin["tripId"]
        await self.redis.lpush(f"trip:{t}:recent", json.dumps(checkin))
        await self.redis.ltrim(f"trip:{t}:recent", 0, 9)
        await self.redis.hset(f"trip:{t}:context", mapping={
            "travellerId": checkin["travellerId"], "stepIndex": checkin["stepIndex"],
            "routeVersion": checkin["routeVersion"], "status": "active"})
        await asyncio.to_thread(self.sqs.send_message, QueueUrl=await self._queue(), MessageBody=json.dumps(checkin))
        await self.redis.set(last_key(t), json.dumps(checkin))

    async def send(self, move: Move) -> dict:
        seq = await self.redis.incr(seq_key(self.s.trip_id))
        checkin = build_checkin(self.s.trip_id, self.s.traveller_id, seq, move, self._route()["version"])
        await self._put(checkin)
        log.info("sent %s#%s step %s at (%s, %s) %.1f m/s", checkin["tripId"], seq, move.step,
                 move.lat, move.lng, move.speed_mps)
        return checkin

    async def _run(self, name: str) -> None:
        try:
            for i, move in enumerate(SCENARIOS[name]["moves"]):
                if i:
                    await asyncio.sleep(self.s.interval_s)
                await self.send(move)
        except Exception:  # noqa: BLE001 - report it; the panel shows "idle" again
            log.exception("scenario %s stopped", name)
        finally:
            self.running = None

    def run(self, name: str) -> int:
        if name not in SCENARIOS:
            raise KeyError(name)
        if self.running:
            raise RuntimeError(self.running)
        self.running = name
        self._task = asyncio.create_task(self._run(name))
        return len(SCENARIOS[name]["moves"])

    async def duplicate(self) -> dict | None:
        raw = await self.redis.get(last_key(self.s.trip_id))
        if not raw:
            return None
        checkin = json.loads(raw)
        await asyncio.to_thread(self.sqs.send_message, QueueUrl=await self._queue(), MessageBody=raw)
        log.info("sent duplicate %s#%s", checkin["tripId"], checkin["seq"])
        return checkin

    async def fault(self, target: str, body: dict) -> tuple[int, dict]:
        """Flip a fake's /control switch. Real detectors have none (404)."""
        try:
            r = await self.http.post(f"http://{target}:8000/control", json=body, timeout=3)
        except httpx.HTTPError as e:
            return 502, {"error": {"code": "unreachable", "message": f"{target}: {type(e).__name__}"}}
        if r.status_code == 404:
            return 409, {"error": {"code": "not_a_fake",
                                   "message": f"{target} is the real service; start the fakes to use this"}}
        return r.status_code, r.json()

    async def _containers(self) -> dict[str, list[dict]]:
        """This compose project's containers, by service name."""
        filters = json.dumps({"label": [f"com.docker.compose.project={self.s.compose_project}"]})
        r = await self.docker.get("/containers/json", params={"all": "true", "filters": filters}, timeout=3)
        r.raise_for_status()
        by_service: dict[str, list[dict]] = {}
        for c in r.json():
            by_service.setdefault(c.get("Labels", {}).get(SERVICE_LABEL), []).append(c)
        return by_service

    async def services(self) -> dict[str, str]:
        """running / exited / ... for each service the panel can stop. Empty without Docker."""
        if self.docker is None:
            return {}
        try:
            found = await self._containers()
        except httpx.HTTPError:
            return {}
        return {name: ("running" if any(c["State"] == "running" for c in found[name]) else found[name][0]["State"])
                for name in sorted(FAULT_TARGETS) if name in found}

    async def switch(self, target: str, action: str) -> tuple[int, dict]:
        """Stop or start a service's container(s): a real outage, not a simulated one."""
        if self.docker is None:
            return 503, {"error": {"code": "no_docker", "message": "the Docker socket isn't mounted"}}
        try:
            containers = (await self._containers()).get(target)
            if not containers:
                return 404, {"error": {"code": "not_found", "message": f"no {target} container in this stack"}}
            for c in containers:
                params = {"t": 2} if action == "stop" else None   # 2 s to shut down cleanly
                r = await self.docker.post(f"/containers/{c['Id']}/{action}", params=params, timeout=15)
                if r.status_code not in (204, 304):                # 304: already stopped / started
                    return 502, {"error": {"code": "docker", "message": f"{action} {target}: HTTP {r.status_code}"}}
        except httpx.HTTPError as e:
            return 502, {"error": {"code": "docker", "message": f"{action} {target}: {type(e).__name__}"}}
        log.info("%s %s", "stopped" if action == "stop" else "started", target)
        return 200, {"target": target, "state": "exited" if action == "stop" else "running"}

    def _clear_incidents(self) -> int:
        n, start = 0, None
        while True:
            page = self.dynamodb.scan(TableName=self.s.incidents_table, ProjectionExpression="incidentId",
                                      **({"ExclusiveStartKey": start} if start else {}))
            for item in page.get("Items", []):
                self.dynamodb.delete_item(TableName=self.s.incidents_table, Key={"incidentId": item["incidentId"]})
                n += 1
            start = page.get("LastEvaluatedKey")
            if not start:
                return n

    async def reset(self) -> None:
        """Back to a clean demo: no decisions, risk or dedup keys; the trip seeded again."""
        if self._task and not self._task.done():
            self._task.cancel()
        self.running = None
        await self.redis.flushdb()
        route = self._route()
        t = self.s.trip_id
        await self.redis.set(f"trip:{t}:route", json.dumps({"steps": route["steps"], "path": route["path"]}), ex=86400)
        await self.redis.hset(f"trip:{t}:context", mapping={
            "travellerId": self.s.traveller_id, "stepIndex": 0, "routeVersion": route["version"], "status": "active"})
        if self.dynamodb is not None:   # the real Alert Service keeps incidents in DynamoDB
            try:
                log.info("cleared %d incidents", await asyncio.to_thread(self._clear_incidents))
            except Exception as e:  # noqa: BLE001 - table not created yet, or the fake is running
                log.warning("couldn't clear the incidents table: %s", e)
        try:   # the fake Alert Service keeps them in memory; the real one answers 404
            await self.http.post("http://alert-service:8000/control/reset", timeout=3)
        except httpx.HTTPError:
            pass
        log.info("demo reset: Redis cleared, %s seeded", t)

    async def status(self) -> dict:
        last = await self.redis.get(seq_key(self.s.trip_id))
        return {"running": self.running, "lastSeq": int(last) if last else 0,
                "trip": self.s.trip_id, "traveller": self.s.traveller_id,
                "scenarios": [{"name": n, "label": s["label"], "checkins": len(s["moves"])} for n, s in SCENARIOS.items()],
                "services": await self.services(),
                "dashboardUrl": self.s.dashboard_url, "caregiverUrl": self.s.caregiver_url}


class RunBody(BaseModel):
    scenario: str


class ServiceBody(BaseModel):
    target: str
    action: str   # "stop" | "start"


class FaultBody(BaseModel):
    target: str
    mode: str | None = None
    delayMs: int | None = Field(default=None, ge=0)


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_app(control: DemoControl | None = None, s: Settings | None = None) -> FastAPI:
    s = s or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.control is not None:   # injected (tests)
            yield
            return
        redis = aioredis.from_url(s.redis_url, decode_responses=True)
        sqs = boto3.client("sqs", region_name=s.aws_region, endpoint_url=s.aws_endpoint_url)
        dynamodb = (boto3.client("dynamodb", region_name=s.aws_region, endpoint_url=s.dynamodb_endpoint_url)
                    if s.dynamodb_endpoint_url else None)
        docker = (httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=s.docker_socket), base_url="http://docker")
                  if Path(s.docker_socket).exists() else None)
        if docker is None:
            log.warning("no Docker socket at %s: stop/start buttons are off", s.docker_socket)
        async with httpx.AsyncClient() as http:
            app.state.control = DemoControl(redis, sqs, http, s, docker=docker, dynamodb=dynamodb)
            yield
        if docker is not None:
            await docker.aclose()
        await redis.aclose()

    app = FastAPI(title="CareRoute demo control (local only)", lifespan=lifespan)
    app.state.control = control

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(PAGE, media_type="text/html")

    @app.get("/api/demo/status")
    async def status(request: Request):
        return await request.app.state.control.status()

    @app.post("/api/demo/run", status_code=202)
    async def run(body: RunBody, request: Request):
        try:
            n = request.app.state.control.run(body.scenario)
        except KeyError:
            return error(404, "unknown_scenario", f"no scenario {body.scenario!r}")
        except RuntimeError as busy:
            return error(409, "busy", f"scenario {busy} is still running")
        return {"scenario": body.scenario, "checkins": n}

    @app.post("/api/demo/duplicate")
    async def duplicate(request: Request):
        checkin = await request.app.state.control.duplicate()
        if checkin is None:
            return error(409, "nothing_sent", "send a check-in first")
        return {"resent": f"{checkin['tripId']}#{checkin['seq']}"}

    @app.post("/api/demo/service")
    async def service(body: ServiceBody, request: Request):
        if body.target not in FAULT_TARGETS:
            return error(404, "unknown_target", f"can't control {body.target!r}")
        if body.action not in ("stop", "start"):
            return error(422, "invalid_request", "action is stop or start")
        code, result = await request.app.state.control.switch(body.target, body.action)
        return JSONResponse(status_code=code, content=result)

    @app.post("/api/demo/fault")
    async def fault(body: FaultBody, request: Request):
        if body.target not in FAULT_TARGETS:
            return error(404, "unknown_target", f"can't control {body.target!r}")
        payload = {k: v for k, v in {"mode": body.mode, "delayMs": body.delayMs}.items() if v is not None}
        if not payload:
            return error(422, "invalid_request", "give mode or delayMs")
        code, result = await request.app.state.control.fault(body.target, payload)
        return JSONResponse(status_code=code, content=result)

    @app.post("/api/demo/reset")
    async def reset(request: Request):
        await request.app.state.control.reset()
        return {"status": "reset"}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
