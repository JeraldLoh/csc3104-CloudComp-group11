"""CareRoute Coordinator (Member 3).

No public API of its own for the pipeline: it consumes the careroute-checkins queue in the
background (consumer.py) and analyses each check-in (pipeline.py).

GET /health   liveness: the process is up (contract endpoint)
GET /ready    readiness: Redis and the queue are reachable
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import boto3
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings, settings as default_settings
from .consumer import SqsConsumer
from .detectors import DetectorClient
from .pipeline import Pipeline

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


def create_app(s: Settings = default_settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        redis = aioredis.from_url(s.redis_url, decode_responses=True)
        sqs = boto3.client("sqs", region_name=s.aws_region, endpoint_url=s.aws_endpoint_url)
        async with httpx.AsyncClient() as http:
            pipeline = Pipeline(redis, http, DetectorClient(http, s.detector_urls, s.detector_timeout_s), s)
            app.state.redis, app.state.sqs = redis, sqs
            app.state.queue_url = await asyncio.to_thread(resolve_queue_url, sqs, s)
            consumer = SqsConsumer(sqs, app.state.queue_url, pipeline, s.max_messages)
            if s.consumer_enabled:
                consumer.start()
            log.info("mode=%s pair=%s quorum=%d aggregator=%s", s.redundancy_mode, ",".join(s.normal_pair),
                     s.quorum, s.aggregator_url)
            yield
            await consumer.stop()
        await redis.aclose()

    app = FastAPI(title="CareRoute Coordinator", lifespan=lifespan)

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
