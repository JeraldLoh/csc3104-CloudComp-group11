"""Alert Service (Member 5): turns problem reports into incidents and alerts caregivers."""
import logging
from contextlib import asynccontextmanager

import boto3
import redis
from fastapi import FastAPI, Response

from app.aws_resources import ensure_tables
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

r = redis.Redis.from_url(settings.redis_url, socket_timeout=2)
dynamodb = boto3.client("dynamodb", region_name=settings.aws_region, endpoint_url=settings.aws_endpoint_url)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.create_resources:
        ensure_tables(dynamodb)
    yield


app = FastAPI(title="Alert Service", lifespan=lifespan)


@app.get("/health")
def health():
    """Process is alive. Doesn't check Redis/DynamoDB, so an outage there doesn't restart every pod."""
    return {"status": "ok"}


@app.get("/ready")
def ready(response: Response):
    """Redis and the incidents table are reachable, so this replica can take traffic."""
    try:
        r.ping()
        # GetItem rather than DescribeTable: the shared AWS role allows GetItem but not DescribeTable.
        dynamodb.get_item(TableName=settings.incidents_table, Key={"incidentId": {"S": "__ready__"}})
    except Exception as e:
        response.status_code = 503
        return {"status": "not_ready", "error": str(e)}
    return {"status": "ready"}
