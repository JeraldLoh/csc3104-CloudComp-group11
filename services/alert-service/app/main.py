"""Alert Service (Member 5): turns problem reports into incidents and alerts caregivers."""
import logging
from contextlib import asynccontextmanager

import boto3
import redis
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.aws_resources import ensure_queue, ensure_tables
from app.config import settings
from app.disruptions import DisruptionConsumer
from app.errors import ApiError
from app.incidents import IncidentStore
from app.models import ProblemReport

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("alert.api")

r = redis.Redis.from_url(settings.redis_url, socket_timeout=2)
dynamodb = boto3.client("dynamodb", region_name=settings.aws_region, endpoint_url=settings.aws_endpoint_url)
sqs = boto3.client("sqs", region_name=settings.aws_region, endpoint_url=settings.aws_endpoint_url)
incidents = IncidentStore(dynamodb)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.create_resources:
        ensure_tables(dynamodb)
        ensure_queue(sqs, settings.disruptions_queue)
    consumer = None
    if settings.disruptions_consumer_enabled:
        queue_url = settings.disruptions_queue_url or sqs.get_queue_url(QueueName=settings.disruptions_queue)["QueueUrl"]
        consumer = DisruptionConsumer(sqs, queue_url, incidents)
        consumer.start()
    yield
    if consumer:
        consumer.stop()


app = FastAPI(title="Alert Service", lifespan=lifespan)


@app.exception_handler(ApiError)
async def api_error(request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    message = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return JSONResponse(status_code=422, content={"error": {"code": "invalid", "message": message}})


def current_user(x_user_id: str | None = Header(default=None)) -> str | None:
    """The caller's user ID. Until login (Cognito, issue 1.5) exists, it comes from the X-User-Id header."""
    return x_user_id


# ---------------------------------------------------------------------------- health
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


# ---------------------------------------------------------------------------- incidents
@app.post("/internal/problems")
def receive_problem(report: ProblemReport):
    """From the Aggregator and the Heartbeat Monitor. Safe to retry: the same problem is one incident."""
    incident, outcome = incidents.record_problem(report)
    log.info("%s %s (detections: %s)", outcome, incident["incidentId"], incident["detectionCount"])
    return {"incidentId": incident["incidentId"], "created": outcome == "created"}


@app.get("/api/incidents")
def list_incidents(travellerId: str, limit: int = Query(50, ge=1, le=200)):
    """A traveller's incidents, newest first."""
    return incidents.list_for_traveller(travellerId, limit)


@app.post("/api/incidents/{incident_id}/ack")
def acknowledge_incident(incident_id: str, user_id: str | None = Depends(current_user)):
    # The ID contains '#', so callers must URL-encode it: U-042%23T-001%23off_route%232
    return incidents.acknowledge(incident_id, user_id)


@app.post("/api/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: str, user_id: str | None = Depends(current_user)):
    return incidents.resolve(incident_id, user_id)
