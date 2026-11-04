"""CareRoute Coordinator (Member 3): the check-in analysis.

No public API of its own for the pipeline: it consumes the careroute-checkins queue in the
background (consumer.py), asks the detectors and decides about each check-in (pipeline.py).

GET /health                        liveness: the process is up (contract endpoint)
GET /ready                         readiness: Redis and the queue are reachable

Dashboard (read-only, from what decision.py has written to Redis):
GET /api/status/travellers         risk per traveller
GET /api/decisions?travellerId=    recent decisions, newest first
GET /dashboard                     the page (polls the two endpoints above)
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import boto3
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from . import dashboard
from .config import Settings, settings as default_settings
from .consumer import SqsConsumer
from .decision import DecisionRecorder
from .detectors import DetectorClient
from .pipeline import Pipeline

DASHBOARD_PAGE = Path(__file__).parent / "static" / "dashboard.html"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("coordinator")


def resolve_queue_url(sqs, s: Settings) -> str:
    if s.queue_url:
        return s.queue_url
    if s.create_queue:   # local development only; in AWS the platform stack creates it
        return sqs.create_queue(QueueName=s.queue_name, Attributes={"VisibilityTimeout": "30"})["QueueUrl"]
    return sqs.get_queue_url(QueueName=s.queue_name)["QueueUrl"]


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_app(s: Settings = default_settings, redis=None) -> FastAPI:
    """`redis` is for tests: the dashboard endpoints then work without the lifespan."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_redis = app.state.redis is None
        if own_redis:
            app.state.redis = aioredis.from_url(s.redis_url, decode_responses=True)
        redis = app.state.redis
        sqs = boto3.client("sqs", region_name=s.aws_region, endpoint_url=s.aws_endpoint_url)
        async with httpx.AsyncClient() as http:
            pipeline = Pipeline(redis, DetectorClient(http, s.detector_urls, s.detector_timeout_s),
                                DecisionRecorder(redis, http, s), s)
            app.state.sqs = sqs
            app.state.queue_url = await asyncio.to_thread(resolve_queue_url, sqs, s)
            consumer = SqsConsumer(sqs, app.state.queue_url, pipeline, s.max_messages)
            if s.consumer_enabled:
                consumer.start()
            log.info("mode=%s pair=%s quorum=%d alerts=%s", s.redundancy_mode, ",".join(s.normal_pair),
                     s.quorum, s.alert_url)
            yield
            await consumer.stop()
        if own_redis:
            await redis.aclose()

    app = FastAPI(title="CareRoute Coordinator", lifespan=lifespan)
    app.state.redis = redis

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in d["loc"][1:]), "problem": d["msg"]} for d in e.errors()]
        return JSONResponse(status_code=422, content={"error": {
            "code": "invalid_request", "message": "Invalid query parameters", "details": details}})

    @app.get("/api/status/travellers")
    async def status_travellers(request: Request):
        return await dashboard.traveller_status(request.app.state.redis)

    @app.get("/api/decisions")
    async def decisions(request: Request, travellerId: str | None = Query(default=None, min_length=1),
                        limit: int = Query(default=50, ge=1, le=200)):
        return await dashboard.decisions(request.app.state.redis, travellerId, limit)

    @app.get("/dashboard", include_in_schema=False)
    def dashboard_page():
        return FileResponse(DASHBOARD_PAGE, media_type="text/html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request):
        failed = []
        try:
            await request.app.state.redis.ping()
        except Exception as e:  # noqa: BLE001 - report every dependency's state
            failed.append(f"redis: {type(e).__name__}")
        try:
            await asyncio.to_thread(request.app.state.sqs.get_queue_attributes,
                                    QueueUrl=request.app.state.queue_url, AttributeNames=["QueueArn"])
        except Exception as e:  # noqa: BLE001
            failed.append(f"sqs: {type(e).__name__}")
        if failed:
            return error(503, "not_ready", "; ".join(failed))
        return {"status": "ready"}

    return app


app = create_app()
