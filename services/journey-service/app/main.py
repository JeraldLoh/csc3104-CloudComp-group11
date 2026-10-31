"""CareRoute Journey Service (Member 6).

POST /api/routes/plan                    route options from OneMap, checked against LTA live data
POST /api/routes/check-disruptions       re-check steps (plan option / saved route) against LTA's alerts now
POST /api/routes                         save a route (version 1, not approved)
GET  /api/routes?travellerId=            a traveller's saved routes (not in the contract yet)
GET/PUT /api/routes/{routeId}            load / edit a route (edit: version + 1, approval cleared)
POST /api/routes/{routeId}/approve       approve a route
POST /api/routes/{routeId}/easy-read     AI wording for every step, for the caregiver to review (nothing saved)
POST /api/routes/{routeId}/photos        upload the caregiver's own photo for a step -> {photoKey, photoUrl}
POST /api/trips                          a trip on an approved route, on a date (planned)
POST /api/trips/{tripId}/start           planned -> active; writes the Redis keys the detectors and checker read
POST /api/trips/{tripId}/end             active -> completed; removes them
GET  /api/trips/{tripId}/pack            the offline route pack in S3: {packUrl, packVersion}
GET  /api/trips/{tripId}, /api/trips?travellerId=   look trips up (not in the contract yet)
GET  /api/places/search?q=               place/address search -> coordinates (OneMap)
GET /health                              liveness: the process is up (contract endpoint)
GET /ready                               readiness: Redis and the routes store are reachable
GET /internal/lta/bus-arrival/{code}     live bus timings at a stop (cluster-only)
GET /internal/lta/train-alerts           current MRT/LRT disruptions (cluster-only)
GET /internal/lta/traffic-incidents      current road incidents (cluster-only)
GET/PUT /api/dev/data-mode               live or saved data, switchable (only with DEMO_UI=true, local demos)

Saved routes and trips live in DynamoDB (ROUTES_STORE=dynamodb) or, with no AWS available, Redis (the
default); packs live in S3. See route_store.py, trip_store.py and pack_store.py.

Every /api/ endpoint knows who is calling (auth.py, AUTH_MODE) and JourneyClient applies the ownership rules:
a route belongs to its caregiver and its traveller, anyone else gets 404. /internal/, /health, /ready and
/api/dev/ are not user endpoints and are not checked.
"""
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import date as date_type

import boto3
import redis
from fastapi import Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import aws_resources
from .auth import AuthUnavailable, Authenticator, Caller, Forbidden, Unauthorized
from .config import Settings, settings as default_settings
from .data_mode import DataMode
from .journey_client import (JourneyClient, OneMapAuthError, OneMapError, OneMapUnavailable, PhotoInvalid,
                             StorageNotReady)
from .lta import LtaAuthError, LtaUnavailable
from .models import DataModeIn, DisruptionCheckIn, PlanRequest, RouteIn, RouteUpdate, TripIn
from .pack_store import PackUnavailable, S3PackStorage
from .replay import RecordedDataMissing, load_manifest
from .route_store import DynamoRouteStore, RouteConflict, RouteInvalid, RouteNotFound
from .trip_store import DynamoTripStore, TripConflict, TripNotFound

ROUTE_ID = re.compile(r"^R-[0-9A-F]{6}$")
TRIP_ID = re.compile(r"^T-[0-9A-F]{6}$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("journey")


def error(status: int, code: str, message: str, details=None) -> JSONResponse:
    """Contract error format: {"error": {"code": ..., "message": ...}}"""
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


def build_journey(s: Settings, redis_client, mode: DataMode | None = None) -> JourneyClient:
    """The client wired to its storage: DynamoDB when ROUTES_STORE=dynamodb (else Redis) and an S3 bucket for
    route packs. CREATE_RESOURCES=true creates the tables and bucket first (local development only)."""
    aws = dict(region_name=s.aws_region, endpoint_url=s.aws_endpoint_url)
    route_store = trip_store = pack_store = None          # None: JourneyClient uses Redis / no packs
    if s.routes_store == "dynamodb":
        if s.create_resources:
            dynamo = boto3.client("dynamodb", **aws)
            aws_resources.ensure_routes_table(dynamo, s.routes_table)
            aws_resources.ensure_trips_table(dynamo, s.trips_table)
        resource = boto3.resource("dynamodb", **aws)
        route_store = DynamoRouteStore(resource, s.routes_table)
        trip_store = DynamoTripStore(resource, s.trips_table)
    if s.packs_bucket:
        pack_store = S3PackStorage.from_settings(s)
        if s.create_resources:
            aws_resources.ensure_packs_bucket(pack_store.client, s.packs_bucket, s.aws_region)
    return JourneyClient(s, redis_client=redis_client, route_store=route_store,
                         trip_store=trip_store, pack_store=pack_store, mode=mode)


def create_app(
    journey: JourneyClient | None = None,
    redis_client: redis.Redis | None = None,
    s: Settings = default_settings,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    auth = authenticator or Authenticator(s)          # a bad AUTH_MODE / Cognito setting fails here, at startup

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        r = redis_client or redis.Redis.from_url(s.redis_url)
        app.state.redis = r
        app.state.mode = mode = DataMode(s, r)
        now = mode.all()
        if "recorded" in now.values():
            log.warning("SAVED-DATA MODE: onemap=%s lta=%s. Answers come from app/saved_data, not the live APIs.",
                        now["onemap"], now["lta"])
        if journey is None:
            app.state.journey = build_journey(s, r, mode)
            # The bus route lists take ~9 s to fetch: get them now, not in the first plan that needs them.
            threading.Thread(target=app.state.journey.lta.warm_bus_services, daemon=True).start()
        else:
            app.state.journey = journey
        yield

    app = FastAPI(title="CareRoute Journey Service", lifespan=lifespan)
    if s.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(s.cors_origins),
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Content-Type", "Authorization"],
            max_age=600,
        )

    def current_caller(request: Request) -> Caller | None:
        """Who is calling: a Caller, or None when AUTH_MODE=off. Raises Unauthorized / AuthUnavailable."""
        return auth.authenticate(request.headers)

    @app.exception_handler(Unauthorized)
    async def _unauthorized(_: Request, e: Unauthorized):
        log.warning("login refused: %s", e)                # the reason is for us, never for the caller
        return error(401, "unauthorized", "Missing or invalid login token")

    @app.exception_handler(Forbidden)
    async def _forbidden(_: Request, e: Forbidden):
        return error(403, "forbidden", str(e))

    @app.exception_handler(AuthUnavailable)
    async def _auth_unavailable(_: Request, e: AuthUnavailable):
        log.error("can't check logins right now: %s", e)
        return error(503, "upstream_unavailable", "Login can't be checked right now. Please retry.")

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in d["loc"][1:]), "problem": d["msg"]} for d in e.errors()]
        return error(422, "invalid_request", "The request doesn't match the contract", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException):
        return error(e.status_code, "not_found" if e.status_code == 404 else "http_error", str(e.detail))

    @app.exception_handler(RouteNotFound)
    async def _route_missing(_: Request, e: RouteNotFound):
        return error(404, "not_found", "No saved route with that id")

    @app.exception_handler(RouteConflict)
    async def _route_conflict(_: Request, e: RouteConflict):
        return error(409, "conflict", "This route was changed by someone else. Reload it and try again.",
                     {"currentVersion": e.current_version})

    @app.exception_handler(TripNotFound)
    async def _trip_missing(_: Request, e: TripNotFound):
        return error(404, "not_found", "No trip with that id")

    @app.exception_handler(TripConflict)
    async def _trip_conflict(_: Request, e: TripConflict):
        return error(409, "conflict", e.message)

    @app.exception_handler(PackUnavailable)
    async def _pack_unavailable(_: Request, e: PackUnavailable):
        log.error("pack/photo storage problem: %s", e)          # packs and step photos share the bucket
        return error(503, "upstream_unavailable", "File storage isn't available right now. Please retry.")

    @app.exception_handler(PhotoInvalid)
    async def _photo_invalid(_: Request, e: PhotoInvalid):
        return error(400, "invalid_photo", str(e))

    @app.exception_handler(RouteInvalid)
    async def _route_invalid(_: Request, e: RouteInvalid):
        return error(422, "invalid_request", str(e))

    @app.exception_handler(LtaUnavailable)
    async def _lta_down(_: Request, e: LtaUnavailable):
        return error(503, "upstream_unavailable", "LTA DataMall is not responding right now")

    @app.exception_handler(LtaAuthError)
    async def _lta_auth(_: Request, e: LtaAuthError):
        # Our misconfiguration, not the caller's. Don't leak details.
        log.error("LTA credentials problem: %s", e)
        return error(502, "upstream_misconfigured", "Live transport data is not available")

    @app.exception_handler(RecordedDataMissing)
    async def _not_saved(_: Request, e: RecordedDataMissing):
        return error(404, "not_found", str(e))

    @app.exception_handler(OneMapUnavailable)
    async def _onemap_down(_: Request, e: OneMapUnavailable):
        return error(503, "upstream_unavailable", "Route planning (OneMap) is not responding right now")

    @app.exception_handler(OneMapAuthError)
    async def _onemap_auth(_: Request, e: OneMapAuthError):
        # Usually the token has expired; the operator must replace ONEMAP_API_KEY.
        log.error("OneMap credentials problem: %s", e)
        return error(502, "upstream_misconfigured", "Route planning is not available")

    @app.exception_handler(OneMapError)
    async def _onemap_error(_: Request, e: OneMapError):
        log.error("OneMap error: %s", e)
        return error(502, "upstream_error", "Route planning could not find a route")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, e: Exception):
        log.exception("unhandled error: %s", e)
        return error(500, "internal_error", "Something went wrong")

    @app.middleware("http")
    async def timing(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - start) * 1000
        response.headers["X-Process-Time-Ms"] = f"{ms:.1f}"
        if request.url.path.startswith(("/api/", "/internal/")):
            log.info("%s %s %d %.1fms", request.method, request.url.path, response.status_code, ms)
        return response

    # Plain `def` endpoints: httpx, redis-py and boto3 are used synchronously, so FastAPI
    # runs these in its thread pool and one slow upstream call doesn't stall other requests.
    @app.post("/api/routes/plan", dependencies=[Depends(current_caller)])      # any logged-in user may plan
    def plan(body: PlanRequest, request: Request):
        return request.app.state.journey.plan(
            (body.from_.lat, body.from_.lng), (body.to.lat, body.to.lng),
            body.departAt, mode=body.mode, max_walk_m=body.maxWalkM, max_options=body.maxOptions, sort_by=body.sortBy,
        )

    @app.post("/api/routes/check-disruptions", dependencies=[Depends(current_caller)])   # any logged-in user
    def check_disruptions(body: DisruptionCheckIn, request: Request):
        """Not in contracts/api.md yet: the disruption warnings for these steps now (plan tab, saved route, trips)."""
        return request.app.state.journey.check_disruptions(body.model_dump(by_alias=True)["steps"])

    def route_id_or_404(route_id: str) -> str:
        if not ROUTE_ID.match(route_id):
            raise RouteNotFound(route_id)
        return route_id

    # Every route answer carries a temporary photoUrl per step (with_photo_urls); it is never stored.
    @app.post("/api/routes", status_code=201)
    def create_route(body: RouteIn, request: Request, caller: Caller | None = Depends(current_caller)):
        journey = request.app.state.journey
        return journey.with_photo_urls(journey.create_route(body.model_dump(by_alias=True), caller))

    @app.get("/api/routes")
    def list_routes(request: Request, travellerId: str = Query(min_length=1, max_length=64),
                    caller: Caller | None = Depends(current_caller)):
        """Not in contracts/api.md yet: a traveller's saved routes, newest first (only those the caller may see)."""
        journey = request.app.state.journey
        return {"routes": [journey.with_photo_urls(r) for r in journey.list_routes(travellerId, caller)]}

    @app.get("/api/routes/{route_id}")
    def get_route(route_id: str, request: Request, caller: Caller | None = Depends(current_caller)):
        journey = request.app.state.journey
        return journey.with_photo_urls(journey.get_route(route_id_or_404(route_id), caller))

    @app.put("/api/routes/{route_id}")
    def update_route(route_id: str, body: RouteUpdate, request: Request,
                     caller: Caller | None = Depends(current_caller)):
        data = body.model_dump(by_alias=True, exclude={"version"})
        journey = request.app.state.journey
        return journey.with_photo_urls(journey.update_route(route_id_or_404(route_id), data, body.version, caller))

    @app.delete("/api/routes/{route_id}", status_code=204)
    def delete_route(route_id: str, request: Request, version: int | None = Query(default=None, ge=1),
                     caller: Caller | None = Depends(current_caller)):
        """Not in contracts/api.md yet. With `version`, only deletes that version (409 if it has changed)."""
        request.app.state.journey.delete_route(route_id_or_404(route_id), version, caller)
        return Response(status_code=204)

    @app.post("/api/routes/{route_id}/approve")
    def approve_route(route_id: str, request: Request, caller: Caller | None = Depends(current_caller)):
        journey = request.app.state.journey
        return journey.with_photo_urls(journey.approve_route(route_id_or_404(route_id), caller))

    @app.post("/api/routes/{route_id}/easy-read")
    def suggest_easy_read(route_id: str, request: Request, caller: Caller | None = Depends(current_caller)):
        """contracts/api.md, Easy-read Service: {"steps": [{stepIndex, easyRead, fallback}], "provider": "ollama" |
        "template"}. Slow (the AI runs on CPU); always answers with usable wording."""
        return request.app.state.journey.suggest_easy_read(route_id_or_404(route_id), caller)

    @app.post("/api/routes/{route_id}/photos", status_code=201)
    def upload_photo(route_id: str, request: Request, photo: UploadFile = File(),
                     caller: Caller | None = Depends(current_caller)):
        """contracts/api.md, step photos: one image (multipart field `photo`). The file is read only after the route
        and the caller are checked, and only one byte past the limit, so a huge upload isn't read whole."""
        return request.app.state.journey.upload_photo(route_id_or_404(route_id), photo.file, caller)

    def trip_id_or_404(trip_id: str) -> str:
        if not TRIP_ID.match(trip_id):
            raise TripNotFound(trip_id)
        return trip_id

    @app.post("/api/trips", status_code=201)
    def create_trip(body: TripIn, request: Request, caller: Caller | None = Depends(current_caller)):
        return request.app.state.journey.create_trip(body.routeId, body.date.isoformat(), caller)

    @app.get("/api/trips")
    def list_trips(request: Request, travellerId: str = Query(min_length=1, max_length=64),
                   date: date_type | None = None, caller: Caller | None = Depends(current_caller)):
        """Not in contracts/api.md yet: a traveller's trips, newest first (only those the caller may see)."""
        return {"trips": request.app.state.journey.list_trips(
            travellerId, date.isoformat() if date else None, caller)}

    @app.get("/api/trips/{trip_id}")
    def get_trip(trip_id: str, request: Request, caller: Caller | None = Depends(current_caller)):
        """Not in contracts/api.md yet."""
        return request.app.state.journey.get_trip(trip_id_or_404(trip_id), caller)

    @app.post("/api/trips/{trip_id}/start")
    def start_trip(trip_id: str, request: Request, caller: Caller | None = Depends(current_caller)):
        return request.app.state.journey.start_trip(trip_id_or_404(trip_id), caller)

    @app.post("/api/trips/{trip_id}/end")
    def end_trip(trip_id: str, request: Request, caller: Caller | None = Depends(current_caller)):
        return request.app.state.journey.end_trip(trip_id_or_404(trip_id), caller)

    @app.get("/api/trips/{trip_id}/pack")
    def trip_pack(trip_id: str, request: Request, caller: Caller | None = Depends(current_caller)):
        return request.app.state.journey.trip_pack(trip_id_or_404(trip_id), caller)

    @app.get("/api/places/search", dependencies=[Depends(current_caller)])     # any logged-in user may search
    def search_places(request: Request, q: str = Query(min_length=2, max_length=100)):
        """Type a place name, address or postal code; get coordinates to plan from/to."""
        return {"places": request.app.state.journey.search_places(q)}

    # Cluster-only helpers: called by other services, not by people, so no user login is asked for.
    @app.get("/internal/lta/bus-arrival/{bus_stop_code}")
    def bus_arrival(bus_stop_code: str, request: Request, service_no: str | None = None):
        return request.app.state.journey.bus_arrival(bus_stop_code, service_no)

    @app.get("/internal/lta/train-alerts")
    def train_alerts(request: Request):
        return request.app.state.journey.train_alerts()

    @app.get("/internal/lta/traffic-incidents")
    def traffic_incidents(request: Request):
        return {"incidents": request.app.state.journey.traffic_incidents()}

    if s.demo_ui:
        # Local demos only (DEMO_UI): switch between live and saved data without a restart (data_mode.py).
        def data_mode_info(request: Request) -> dict:
            info = {**request.app.state.mode.all(), "onemapRecordedAt": None, "ltaRecordedAt": None, "trips": []}
            try:
                saved = load_manifest()
            except RecordedDataMissing:
                return info                                    # nothing captured yet: only live data is possible
            info["onemapRecordedAt"] = (saved.get("onemap") or {}).get("recordedAt")
            info["ltaRecordedAt"] = ((saved.get("lta") or {}).get("recordedAt") or "")[:10] or None
            info["trips"] = saved.get("trips", [])
            return info

        @app.get("/api/dev/data-mode", include_in_schema=False)
        def get_data_mode(request: Request):
            """Which data is in use, when the saved data was captured, and the trips it can plan."""
            return data_mode_info(request)

        @app.put("/api/dev/data-mode", include_in_schema=False)
        def put_data_mode(body: DataModeIn, request: Request):
            request.app.state.mode.set(onemap=body.onemap, lta=body.lta)
            log.warning("data mode switched: %s", request.app.state.mode.all())
            return data_mode_info(request)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request):
        try:
            request.app.state.redis.ping()
        except Exception as e:  # noqa: BLE001
            return error(503, "not_ready", f"redis: {type(e).__name__}")
        try:
            request.app.state.journey.storage_ping()
        except StorageNotReady as e:                       # names the store and the AWS error code, nothing else
            return error(503, "not_ready", str(e))
        except Exception as e:  # noqa: BLE001
            return error(503, "not_ready", f"storage: {type(e).__name__}")
        return {"status": "ready"}

    return app


app = create_app()
