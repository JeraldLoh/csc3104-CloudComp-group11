"""Alert Service (Member 5): turns problem reports into incidents and alerts caregivers, and keeps
caregivers' profiles: their travellers and those travellers' safe locations (issue 5.1).

Who may see what: with a login (AUTH_MODE dev or cognito, see auth.py) a caregiver only reaches travellers
they're linked to (caregiver-links). Anyone else gets "not found", so they can't tell a traveller exists.
With AUTH_MODE=off nothing is checked, except that profiles need to know which caregiver is asking.
"""
import logging
from contextlib import asynccontextmanager

import boto3
import redis
import redis.asyncio as redis_async
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader

from app.auth import AuthUnavailable, Authenticator, Caller, Forbidden, Unauthorized
from app.aws_resources import ensure_queue, ensure_tables
from app.config import settings
from app.disruptions import DisruptionConsumer
from app.errors import ApiError
from app.events import EVENT_FOR_OUTCOME, AlertPublisher
from app.incidents import IncidentStore
from app.models import NewLink, NewSafeLocation, NewTraveller, ProblemReport
from app.profiles import ProfileStore
from app.stream import event_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("alert.api")

r = redis.Redis.from_url(settings.redis_url, socket_timeout=2)
r_async = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)  # for /api/stream
dynamodb = boto3.client("dynamodb", region_name=settings.aws_region, endpoint_url=settings.aws_endpoint_url)
sqs = boto3.client("sqs", region_name=settings.aws_region, endpoint_url=settings.aws_endpoint_url)
incidents = IncidentStore(dynamodb)
profiles = ProfileStore(dynamodb)
publisher = AlertPublisher(r, dynamodb, settings.travellers_table)
authenticator = Authenticator(settings)  # a bad AUTH_MODE / Cognito setting fails here, at startup


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.create_resources:
        ensure_tables(dynamodb)
        ensure_queue(sqs, settings.disruptions_queue)
    consumer = None
    if settings.disruptions_consumer_enabled:
        queue_url = settings.disruptions_queue_url or sqs.get_queue_url(QueueName=settings.disruptions_queue)["QueueUrl"]
        consumer = DisruptionConsumer(sqs, queue_url, incidents, publisher=publisher)
        consumer.start()
    yield
    if consumer:
        consumer.stop()


app = FastAPI(title="Alert Service", lifespan=lifespan)


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(ApiError)
async def api_error(request: Request, exc: ApiError):
    return error(exc.status, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    return error(422, "invalid", "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()))


@app.exception_handler(Unauthorized)
async def unauthorized(request: Request, exc: Unauthorized):
    log.info("unauthorized: %s", exc)  # the reason is for the log only
    return error(401, "unauthorized", "Missing or invalid login token")


@app.exception_handler(Forbidden)
async def forbidden(request: Request, exc: Forbidden):
    return error(403, "forbidden", str(exc))


@app.exception_handler(AuthUnavailable)
async def auth_unavailable(request: Request, exc: AuthUnavailable):
    log.warning("can't check logins: %s", exc)
    return error(503, "unavailable", "Can't check the login right now; try again shortly")


# ---------------------------------------------------------------------------- who may see what
# Declared only so /docs shows an "Authorize" box for the dev login; auth.py reads the headers itself.
_dev_user_header = APIKeyHeader(name="X-Dev-User", auto_error=False, description="AUTH_MODE=dev: e.g. G-003")


def caller(request: Request, _: str | None = Depends(_dev_user_header)) -> Caller | None:
    """Who is calling: a Caller, or None when AUTH_MODE=off."""
    return authenticator.authenticate(request.headers)


def require_caregiver(who: Caller | None, action: str) -> Caller:
    """Profiles belong to a caregiver, so these endpoints need to know which one, even with AUTH_MODE=off."""
    if who is None:
        raise ApiError(401, "unauthorized", "This needs a login: run with AUTH_MODE=dev (X-Dev-User header) or cognito")
    if not who.is_caregiver:
        raise Forbidden(f"Only a caregiver can {action}.")
    return who


def check_access(who: Caller | None, traveller_id: str, allow_self: bool = False,
                 not_found: str | None = None) -> None:
    """A caregiver only reaches their linked travellers; with `allow_self`, a traveller reaches their own data.
    Everyone else gets "not found"."""
    if who is None:
        return
    if who.is_caregiver and profiles.is_linked(who.id, traveller_id):
        return
    if allow_self and not who.is_caregiver and who.id == traveller_id:
        return
    raise ApiError(404, "not_found", not_found or f"Traveller {traveller_id} not found")


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
    publisher.publish(EVENT_FOR_OUTCOME[outcome], incident)
    return {"incidentId": incident["incidentId"], "created": outcome == "created"}


@app.get("/api/incidents")
def list_incidents(travellerId: str, limit: int = Query(50, ge=1, le=200), who: Caller | None = Depends(caller)):
    """A traveller's incidents, newest first."""
    check_access(who, travellerId)
    return incidents.list_for_traveller(travellerId, limit)


def _incident_for(who: Caller | None, incident_id: str) -> None:
    """Not found if it doesn't exist or isn't one of the caller's travellers (same answer either way)."""
    traveller_id = incidents.get(incident_id)["travellerId"]
    check_access(who, traveller_id, not_found=f"Incident {incident_id} not found")


@app.post("/api/incidents/{incident_id}/ack")
def acknowledge_incident(incident_id: str, who: Caller | None = Depends(caller)):
    # The ID contains '#', so callers must URL-encode it: U-042%23T-001%23off_route%232
    _incident_for(who, incident_id)
    incident = incidents.acknowledge(incident_id, who.id if who else None)
    publisher.publish("acknowledged", incident)
    return incident


@app.post("/api/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: str, who: Caller | None = Depends(caller)):
    _incident_for(who, incident_id)
    incident = incidents.resolve(incident_id, who.id if who else None)
    publisher.publish("resolved", incident)
    return incident


# ---------------------------------------------------------------------------- live alerts
@app.get("/api/stream")
async def stream(request: Request, travellerId: str | None = None,
                 devUser: str | None = None, token: str | None = None):
    """Live alert-event.json messages as Server-Sent Events. Optional: only one traveller's.

    A browser's EventSource can't send headers, so the login may also come as ?devUser=G-003
    (AUTH_MODE=dev) or ?token=<Cognito JWT>."""
    headers = dict(request.headers)
    if devUser:
        headers["x-dev-user"] = devUser
    if token:
        headers["authorization"] = f"Bearer {token}"
    who = authenticator.authenticate(headers)
    load_allowed = None
    if who is not None:
        if not who.is_caregiver:
            raise Forbidden("Only a caregiver can watch alerts.")
        load_allowed = lambda: set(profiles.linked_traveller_ids(who.id))  # noqa: E731
    return StreamingResponse(
        event_stream(r_async, request, travellerId, load_allowed=load_allowed),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},  # don't let proxies buffer it
    )


# ---------------------------------------------------------------------------- profiles (issue 5.1)
@app.get("/api/travellers")
def my_travellers(who: Caller | None = Depends(caller)):
    """The calling caregiver's travellers."""
    return profiles.linked_travellers(require_caregiver(who, "list travellers").id)


@app.post("/api/travellers", status_code=201)
def add_traveller(body: NewTraveller, who: Caller | None = Depends(caller)):
    """A new traveller, linked to the caregiver who adds them."""
    me = require_caregiver(who, "add a traveller")
    traveller = profiles.create_traveller(me.id, body.name, body.preferences)
    log.info("%s added traveller %s", me.id, traveller["travellerId"])
    return traveller


@app.delete("/api/travellers/{travellerId}")
def remove_traveller(travellerId: str, who: Caller | None = Depends(caller)):
    """Take a traveller off the caller's list. If nobody else looks after them, their profile is deleted too."""
    me = require_caregiver(who, "remove a traveller")
    deleted = profiles.remove_traveller(me.id, travellerId)
    log.info("%s removed traveller %s (profile deleted: %s)", me.id, travellerId, deleted)
    return {"travellerId": travellerId, "removed": True, "profileDeleted": deleted}


@app.post("/api/links", status_code=201)
def add_link(body: NewLink, who: Caller | None = Depends(caller)):
    """Add another caregiver to a traveller. The caller must already be linked to that traveller."""
    me = require_caregiver(who, "link caregivers")
    return profiles.add_link(me.id, body.travellerId, body.caregiverId)


@app.get("/api/travellers/{travellerId}/safe-locations")
def list_safe_locations(travellerId: str, who: Caller | None = Depends(caller)):
    check_access(who, travellerId, allow_self=True)
    return profiles.safe_locations(travellerId)


@app.post("/api/travellers/{travellerId}/safe-locations", status_code=201)
def add_safe_location(travellerId: str, body: NewSafeLocation, who: Caller | None = Depends(caller)):
    check_access(who, travellerId)
    return profiles.add_safe_location(travellerId, body.name, body.lat, body.lng, who.id if who else None)


@app.get("/internal/links")
def check_link(caregiverId: str, travellerId: str):
    """For other services in the cluster (e.g. the Journey Service): is this caregiver linked to this traveller?"""
    return {"linked": profiles.is_linked(caregiverId, travellerId)}
