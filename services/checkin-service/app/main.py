"""CareRoute Check-in Service (Member 2, issue 2.3).

POST /api/checkins        one check-in        -> 202 {"accepted": 1, "duplicates": 0}
POST /api/checkins/batch  check-ins buffered offline
GET  /health              liveness: the process is up (contract endpoint)
GET  /ready               readiness: Redis, DynamoDB and SQS are reachable
GET  /metrics             Prometheus metrics
"""
import logging
import os
import time
from contextlib import asynccontextmanager

import boto3
import redis
from botocore.config import Config
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import aws_resources, metrics
from .config import Settings, settings as default_settings
from .live_state import LiveState
from .models import Checkin, CheckinBatch
from .queue import CheckinQueue
from .reconciler import LeaderLease, Reconciler, replica_id
from .service import CheckinService
from .store import CheckinStore

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("checkin")


def build_service(s: Settings) -> CheckinService:
    # boto3 keeps 10 connections by default, but up to MAX_IN_FLIGHT requests (default 32)
    # run at once; with too few, connections are thrown away and re-opened under load.
    pool = Config(max_pool_connections=s.max_in_flight + 8, retries={"mode": "standard"})
    aws = dict(region_name=s.aws_region, endpoint_url=s.aws_endpoint_url, config=pool)
    if s.create_resources:
        aws_resources.ensure_table(boto3.client("dynamodb", **aws), s.table_name)
        aws_resources.ensure_queues(boto3.client("sqs", **aws), s.queue_name)
    return CheckinService(
        store=CheckinStore(boto3.resource("dynamodb", **aws), s.table_name, s.checkin_ttl_days),
        state=LiveState(redis.Redis.from_url(s.redis_url), s.trip_key_ttl_s, s.recent_max, s.intervals_max),
        queue=CheckinQueue(boto3.client("sqs", **aws), s.queue_name),
    )


KNOWN_PATHS = {"/api/checkins", "/api/checkins/batch"}


def error(status: int, code: str, message: str, details=None) -> JSONResponse:
    """Contract error format: {"error": {"code": ..., "message": ...}}"""
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


class AuthError(Exception):
    pass


def create_app(service: CheckinService | None = None, s: Settings = default_settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service or build_service(s)
        app.state.service = svc
        app.state.reconciler = None
        if s.reconciler_enabled:
            lease = LeaderLease(svc.state.r, replica_id(), s.leader_lease_ms)
            rec = Reconciler(svc, lease, s.reconcile_interval_s, s.reconcile_grace_s, s.reconcile_batch)
            rec.start()
            app.state.reconciler = rec
        yield
        if app.state.reconciler:
            await app.state.reconciler.stop()

    app = FastAPI(title="CareRoute Check-in Service", lifespan=lifespan)
    if s.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(s.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "Authorization"],
            max_age=600,
        )

    def require_token(authorization: str | None = Header(default=None)) -> None:
        # TODO(issue 1.5): verify the Cognito JWT and check the traveller owns this trip.
        if s.auth_required and not (authorization or "").startswith("Bearer "):
            raise AuthError()

    @app.exception_handler(AuthError)
    async def _auth(_: Request, __: AuthError):
        return error(401, "unauthorized", "Missing or invalid login token")

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in d["loc"][1:]), "problem": d["msg"]} for d in e.errors()]
        return error(422, "invalid_request", "The check-in doesn't match contracts/examples/checkin.json", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException):
        return error(e.status_code, "not_found" if e.status_code == 404 else "http_error", str(e.detail))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, e: Exception):
        # Redis, DynamoDB or SQS failed part-way. 503 tells the app to keep the
        # check-in in its outbox and retry; the retry finishes the job (see service.py).
        log.exception("check-in failed: %s", e)
        return error(503, "unavailable", "Couldn't store the check-in right now. Please retry.")

    # Requests being handled right now. Only touched on the event loop thread, so a plain
    # int is safe (no lock needed): all middleware code runs on that one thread.
    in_flight = {"n": 0}

    @app.middleware("http")
    async def admission_and_timing(request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        # Fixed label values, so unknown URLs can't create unlimited metric series.
        path = request.url.path if request.url.path in KNOWN_PATHS else "other"
        # Overload protection (load shedding). Answering 503 immediately is kinder than
        # queueing: the phone keeps the check-in in its outbox and retries after
        # Retry-After (plus random jitter), instead of waiting until its own timeout.
        if in_flight["n"] >= s.max_in_flight:
            metrics.SHED.inc()
            metrics.REQUESTS.labels(path, "503").inc()
            resp = error(503, "overloaded", "Too many requests right now. Please retry shortly.")
            resp.headers["Retry-After"] = str(s.retry_after_s)
            return resp
        in_flight["n"] += 1
        metrics.IN_FLIGHT.set(in_flight["n"])
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            in_flight["n"] -= 1
            metrics.IN_FLIGHT.set(in_flight["n"])
        secs = time.perf_counter() - start
        response.headers["X-Process-Time-Ms"] = f"{secs * 1000:.1f}"
        metrics.REQUESTS.labels(path, str(response.status_code)).inc()
        metrics.REQUEST_SECONDS.labels(path).observe(secs)
        log.info("%s %s %d %.1fms", request.method, request.url.path, response.status_code, secs * 1000)
        return response

    # Plain `def` endpoints: boto3 and redis-py are blocking, so FastAPI runs these in
    # its thread pool and one slow call doesn't stall other requests.
    @app.post("/api/checkins", status_code=202, dependencies=[Depends(require_token)])
    def post_checkin(body: Checkin, request: Request):
        return request.app.state.service.ingest_one(body).body()

    @app.post("/api/checkins/batch", status_code=202, dependencies=[Depends(require_token)])
    def post_batch(body: CheckinBatch, request: Request):
        return request.app.state.service.ingest_batch(body.expand(), budget_s=s.batch_budget_ms / 1000).body()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/metrics")
    def prometheus_metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/ready")
    def ready(request: Request):
        svc: CheckinService = request.app.state.service
        failed = []
        for name, check in (("redis", svc.state.ping), ("dynamodb", svc.store.ping), ("sqs", svc.queue.ping)):
            try:
                check()
            except Exception as e:  # noqa: BLE001 - report every dependency's state
                failed.append(f"{name}: {type(e).__name__}")
        if failed:
            return error(503, "not_ready", "; ".join(failed))
        return {"status": "ready"}

    return app


app = create_app()
