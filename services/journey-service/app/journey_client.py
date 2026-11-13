"""JourneyClient: what the API calls (main.py). Saved routes, trips and route packs are here; the rest is done by
the parts it holds, and the client keeps a method of the same name for each thing the API or a test uses:

    client = JourneyClient(settings, redis_client=redis)

    client.onemap    onemap.py        places, station exits, buildings, raw routes     client.search_places(...)
    client.lta       lta.py           LTA DataMall (shared with the Lambda)            client.bus_arrival(...)
    client.planner   planning.py      route options and their live warnings            client.plan(...)
    client.photos    photos.py        Street View and uploaded step photos, links       client.upload_photo(...)
    (mixed in)       trip_changes.py  bridging buses and re-routes for a trip under way client.watch_bridges(...)

    client.create_route / create_trip / start_trip / trip_pack ...   saved routes, trips, the offline pack

The disruption checker (disruption.py) runs in the Lambda, which must not need this file's login, route and pack
code (a test checks it imports none of it).

Failures raise LtaError / OneMapError subclasses; plan() degrades instead when only LTA is down.
Redis is optional here (a cache for lookups; stores for routes and trips when no DynamoDB table is configured).
"""
import json
import logging
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Callable

import httpx

from . import easy_read_ai, landmarks
from .auth import Caller, Forbidden
from .cache import JsonCache
from .config import Settings
from .easy_read import easy_read, plain, with_easy_read
from .lta import LtaClient
from .onemap import OneMapClient
from .photos import RoutePhotos
from .planning import Planner
from .timefmt import iso_utc
from .trip_changes import TripChanges
from .pack_store import PackUnavailable
from .route_store import RedisRouteStore, RouteExists, RouteInvalid, RouteNotFound
from .trip_store import RedisTripStore, TripConflict, TripExists, TripNotFound

log = logging.getLogger("journey")



# Standard texts for the traveller's phone
PACK_MESSAGES = {
    "somethingChanged": "Something has changed. Stay where you are. Your caregiver has been told.",
    "offRoute": "You may be going the wrong way. Stop and look at the next picture.",
    "help": "Press the big red button if you need help.",
}


# Exceptions ---------------------------------------------------------------------------
class StorageNotReady(Exception):
    """A store the service needs can't be used. `label` says which one, `code` is the AWS error code
    (e.g. ResourceNotFoundException, AccessDeniedException) and nothing more, so /ready can show it safely."""

    def __init__(self, label: str, code: str):
        super().__init__(f"{label}: {code}")
        self.label, self.code = label, code


# Client ------------------------------------------------------------------------

class JourneyClient(TripChanges):
    """Trip changes (bridging buses, re-routes) are in trip_changes.py, mixed in here."""

    def __init__(
        self,
        settings: Settings,
        redis_client=None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        lta_http: httpx.Client | None = None,
        onemap_http: httpx.Client | None = None,
        route_store=None,
        trip_store=None,
        pack_store=None,
        lta: LtaClient | None = None,
        mode=None,                                   # data_mode.DataMode; None = ONEMAP_MODE / LTA_MODE, fixed
        easyread_http: httpx.Client | None = None,   # the Easy-read Service (tests pass a mocked one)
        notify: Callable[[dict], None] | None = None,  # tells the caregiver (careroute-disruptions, see trip changes)
        photo_http: httpx.Client | None = None,      # Google Street View
        speech=None,                                 # speech.Speech: spoken instructions in packs; None = none
    ):
        self._s = settings
        self._r = redis_client
        # Saved routes and trips: DynamoDB, or Redis when no table is configured (see route_store.py).
        self._routes = route_store or (RedisRouteStore(redis_client) if redis_client is not None else None)
        self._trips = trip_store or (RedisTripStore(redis_client) if redis_client is not None else None)
        self._packs = pack_store                     # S3; None = packs unavailable
        self._storage_ok_until = 0.0                 # time.monotonic() until which storage_ping() is skipped
        self._sleep = sleep
        self._clock = clock
        self._cache = JsonCache(redis_client)
        # Live or saved data (data_mode.py): read on every call, so a switch takes effect without a restart.
        self._mode = mode
        # LTA data comes from the shared client; the API calls it through the methods below.
        self.lta = lta or LtaClient(settings, redis_client=redis_client, sleep=sleep, clock=clock, http=lta_http,
                                    mode=mode)
        # OneMap's places, exits, buildings and routes (onemap.py), like self.lta for LTA
        self.onemap = OneMapClient(settings, redis_client, sleep=sleep, clock=clock, http=onemap_http, mode=mode)
        # Route planning (planning.py); it asks this client for station points, so a test can replace them
        self.planner = Planner(settings, self.lta, self.onemap, clock, lambda station: self._station_point(station))
        self._easyread_http = easyread_http or httpx.Client()
        self._notify = notify
        # Step photos (photos.py); station exits come from this client, so a test can replace them
        self.photos = RoutePhotos(settings, pack_store, photo_http or httpx.Client(),
                                  lambda name: self.station_exits(name))
        self._speech = speech

    # LTA DataMall (the work is done by self.lta, see lta.py) =====

    def bus_stops(self) -> list[dict]:
        return self.lta.bus_stops()

    def bus_stop(self, code: str) -> dict | None:
        return self.lta.bus_stop(code)

    def bus_arrival(self, bus_stop_code: str, service_no: str | None = None) -> dict:
        return self.lta.bus_arrival(bus_stop_code, service_no)

    def train_alerts(self) -> dict:
        return self.lta.train_alerts()

    def traffic_incidents(self) -> list[dict]:
        return self.lta.traffic_incidents()

    # OneMap (the work is done by self.onemap, see onemap.py) =====

    NO_EXITS_TTL_S = OneMapClient.NO_EXITS_TTL_S

    def search_places(self, query: str, limit: int = 10) -> list[dict]:
        return self.onemap.search_places(query, limit)

    def station_exits(self, station_name: str) -> list[tuple[float, float]]:
        return self.onemap.station_exits(station_name)

    def nearby_buildings(self, lat: float, lng: float) -> list[dict]:
        return self.onemap.nearby_buildings(lat, lng)

    def route_landmarks(self, route: dict) -> dict[int, dict[str, list[dict]]]:
        """What the traveller will see at each spot of the route (landmarks.py), the lookups run at once."""
        found = landmarks.spots(route)
        points = sorted({(s.lat, s.lng) for by_spot in found.values() for s in by_spot.values()})
        if not points:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(points))) as pool:
            buildings = dict(zip(points, pool.map(lambda p: self.nearby_buildings(*p), points)))
        return landmarks.offer(route, found, lambda lat, lng: buildings[(lat, lng)])

    def route_pt(self, start: tuple[float, float], end: tuple[float, float], when: datetime,
                 mode: str = "TRANSIT", max_walk_m: int | None = None, num_itineraries: int | None = None) -> dict:
        return self.onemap.route_pt(start, end, when, mode, max_walk_m, num_itineraries)

    # Planning (the work is done by self.planner, see planning.py) =====

    def plan(self, origin: tuple[float, float], destination: tuple[float, float], when: datetime | None = None,
             mode: str = "any", max_walk_m: int | None = None, max_options: int | None = None,
             sort_by: str = "walking") -> dict:
        return self.planner.plan(origin, destination, when, mode, max_walk_m, max_options, sort_by)

    def check_disruptions(self, steps: list[dict]) -> dict:
        return self.planner.check_disruptions(steps)

    # Saved routes and trips
    #
    # A route is route.json: the caregiver's chosen plan option plus names. `version` goes up by one on
    # every edit and an edit clears `approved`, so a caregiver must re-approve what they changed. Approving
    # doesn't change the content, so it keeps the version (a trip pins `routeVersion`). Two people editing at
    # once can't overwrite each other: update_route needs the version you edited, else it raises RouteConflict.

    def _route_store(self):
        if self._routes is None:
            raise RuntimeError("saved routes need a Redis client (or another route store)")
        return self._routes

    def storage_ping(self) -> None:
        """Raises StorageNotReady (naming the store and the AWS error code) if the routes table, trips table or pack
        bucket can't be used. Redis stores have no ping (the Redis check covers them). A success is remembered
        for READY_CACHE_S seconds; a failure never is."""
        if time.monotonic() < self._storage_ok_until:
            return
        for label, store in (("routes table", self._routes), ("trips table", self._trips), ("pack bucket", self._packs)):
            ping = getattr(store, "ping", None)
            if not ping:
                continue
            try:
                ping()
            except Exception as e:  # noqa: BLE001 - whatever the AWS client raised, report which store and why
                code = getattr(e, "response", {}).get("Error", {}).get("Code") or type(e).__name__
                raise StorageNotReady(label, str(code)) from e
        self._storage_ok_until = time.monotonic() + self._s.ready_cache_s

    def _now_iso(self) -> str:
        return iso_utc(self._clock())

    # Who may touch what. Every public route/trip method takes an optional `caller` (auth.Caller); None means
    # auth is off and nothing is checked. A route belongs to its caregiver (full control) and its traveller
    # (read, and run their own trips). Anyone else gets "not found", so they can't tell a route exists.
    # Not checked yet: that the caregiver is really linked to the traveller; that needs the Profiles service.

    @staticmethod
    def _owns(caller: Caller, route: dict) -> bool:
        return caller.is_caregiver and route["caregiverId"] == caller.id

    @staticmethod
    def _participates(caller: Caller, route: dict) -> bool:
        return route["caregiverId" if caller.is_caregiver else "travellerId"] == caller.id

    def _may_see_trip(self, caller: Caller, trip: dict, routes: dict | None = None) -> bool:
        if not caller.is_caregiver:
            return trip["travellerId"] == caller.id
        route = (routes if routes is not None else {}).get(trip["routeId"]) or self._route_store().get(trip["routeId"])
        if routes is not None and route is not None:
            routes[trip["routeId"]] = route
        return route is not None and route["caregiverId"] == caller.id

    def create_route(self, data: dict, caller: Caller | None = None) -> dict:
        """Saves a new route (unapproved, version 1) and returns it with its new routeId."""
        if caller is not None:
            if not caller.is_caregiver:
                raise Forbidden("Only a caregiver can save a route.")
            if data.get("caregiverId") != caller.id:
                raise Forbidden("A route can only be saved as yourself: caregiverId must be your own id.")
        store = self._route_store()
        route = {**self.photos.add_street_photos(with_easy_read(data)), "version": 1, "approved": False,
                 "updatedAt": self._now_iso()}
        for _ in range(5):                       # ids are random; a clash is vanishingly rare
            candidate = self._ordered({**route, "routeId": "R-" + secrets.token_hex(3).upper()})
            try:
                store.create(candidate)
                return candidate
            except RouteExists:
                continue
        raise RuntimeError("could not allocate a route id")

    def get_route(self, route_id: str, caller: Caller | None = None) -> dict:
        route = self._route_store().get(route_id)
        if route is None or (caller is not None and not self._participates(caller, route)):
            raise RouteNotFound(route_id)
        return route

    def update_route(self, route_id: str, data: dict, expected_version: int, caller: Caller | None = None) -> dict:
        """Replaces the route's content. Raises RouteConflict if `expected_version` is out of date."""
        current = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, current):
            raise Forbidden("Only the route's caregiver can change it.")
        for field in ("travellerId", "caregiverId"):
            if data.get(field) != current[field]:
                raise RouteInvalid(f"{field} can't be changed on a saved route")
        route = self._ordered({**self.photos.add_street_photos(with_easy_read(data)), "routeId": route_id,
                               "version": current["version"] + 1, "approved": False, "updatedAt": self._now_iso()})
        self._route_store().replace(route, expected_version)
        return route

    def approve_route(self, route_id: str, caller: Caller | None = None) -> dict:
        """Marks the route approved (idempotent). The content and version are unchanged."""
        current = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, current):
            raise Forbidden("Only the route's caregiver can approve it.")
        if current["approved"]:
            return current
        # Read the steps aloud now, once (speech.py): only approved routes get trips, and an approved route's
        # wording is final, so its packs find the audio ready instead of waiting for Polly.
        self._speak(current["steps"])
        route = {**current, "approved": True, "updatedAt": self._now_iso()}
        self._route_store().replace(route, current["version"])
        return route

    def delete_route(self, route_id: str, expected_version: int | None = None, caller: Caller | None = None) -> None:
        """Removes a saved route. Raises RouteConflict if `expected_version` is given and out of date, and
        TripConflict while one of its trips is under way. Its planned trips stay but can no longer start."""
        current = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, current):
            raise Forbidden("Only the route's caregiver can delete it.")
        if self._trips is not None and any(
                t["routeId"] == route_id and t["status"] == "active"
                for t in self._trips.list_for_traveller(current["travellerId"])):
            raise TripConflict(f"Route {route_id} has a trip under way. End that trip before deleting the route.")
        self._route_store().delete(route_id, expected_version)

    def list_routes(self, traveller_id: str, caller: Caller | None = None) -> list[dict]:
        """A traveller's saved routes, most recently changed first. With a caller, only the ones that caller may
        see: asking for someone else's traveller gives an empty list, not an error."""
        routes = self._route_store().list_for_traveller(traveller_id)
        if caller is not None:
            routes = [r for r in routes if self._participates(caller, r)]
        return sorted(routes, key=lambda r: r["updatedAt"], reverse=True)

    # Step photos (the work is done by self.photos, see photos.py) =====

    def with_photo_urls(self, route: dict) -> dict:
        """The route as the API returns it: each step, and each of its landmarks, with a temporary `photoUrl`."""
        return self.photos.with_urls(route)

    def upload_photo(self, route_id: str, photo: bytes | BinaryIO, caller: Caller | None = None) -> dict:
        """Stores a caregiver's photo for a step: {"photoKey", "photoUrl"}. The route itself is not changed: the
        editor puts the key in the step and the caregiver's save stores it (so Cancel really discards it).
        A file is only read once the route and the caller are checked (photos.store_upload)."""
        route = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, route):
            raise Forbidden("Only the route's caregiver can change its photos.")
        return self.photos.store_upload(photo)

    def suggest_easy_read(self, route_id: str, caller: Caller | None = None) -> dict:
        """AI wording for every step of a saved route, for the caregiver to check and edit before saving. Nothing
        is saved. Template wording (provider "template") when the Easy-read Service can't help (easy_read_ai.py)."""
        route = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, route):
            raise Forbidden("Only the route's caregiver can reword it.")
        found = self.route_landmarks(route) if self._s.easyread_url and self._s.easyread_landmarks else {}
        answer = easy_read_ai.suggest(route["steps"], self._easyread_http, self._s.easyread_url,
                                      self._s.easyread_timeout_s, found, (route.get("destination") or {}).get("name"))
        # A photo of each landmark the wording names, so the caregiver sees what the traveller will look for
        # before saving (each new one is a paid Street View image; the same place is fetched only once).
        if self.photos.enabled:
            self.photos.photograph_landmarks(route, answer["steps"])
        for s in answer["steps"]:
            s["landmarks"] = [{**m, "photoKey": m.get("photoKey"), "sameView": m.get("sameView"),
                               "photoUrl": self.photos.photo_url(m.get("photoKey"))} for m in s["landmarks"]]
        return answer

    @staticmethod
    def _ordered(route: dict) -> dict:
        """Same field order as contracts/examples/route.json, for readable output."""
        head = ["routeId", "travellerId", "caregiverId", "name", "version", "approved",
                "origin", "destination", "steps", "path", "updatedAt"]
        return {**{k: route[k] for k in head if k in route}, **{k: v for k, v in route.items() if k not in head}}

    # Trips 
    #
    # A trip is one journey on one day along an approved route. It pins the route's version, because a
    # disruption or an edit must never change a journey that is already under way. Statuses:
    # planned -> active -> completed. Starting a trip writes what the rest of the system reads
    # (contracts/redis-keys.md): the trip id in the set `active-trips` and its steps in `trip:{id}:route`.

    def _trip_store(self):
        if self._trips is None:
            raise RuntimeError("trips need a Redis client (or another trip store)")
        return self._trips

    def _redis(self):
        if self._r is None:
            raise RuntimeError("starting and ending trips needs a Redis client")
        return self._r

    def _public_trip(self, trip: dict) -> dict:
        # expiresAt is only for DynamoDB's TTL; planSteps (the whole changed plan) is in the pack, `override` says what
        # changed (its steps with a temporary photoUrl each, as a route's).
        out = {k: v for k, v in trip.items() if k not in ("expiresAt", "planSteps", "planPaths")}
        if out.get("override"):
            out["override"] = {**out["override"], "steps": [self.photos.step_with_urls(s) for s in out["override"]["steps"]]}
        return out

    def _stored_trip(self, trip_id: str, caller: Caller | None = None) -> dict:
        """The trip, or TripNotFound; also when `caller` may not see it (the route's caregiver or the trip's
        traveller may)."""
        trip = self._trip_store().get(trip_id)
        if trip is None or (caller is not None and not self._may_see_trip(caller, trip)):
            raise TripNotFound(trip_id)
        return trip

    def create_trip(self, route_id: str, trip_date: str, caller: Caller | None = None) -> dict:
        """A planned trip on an approved route. `trip_date` is "YYYY-MM-DD"."""
        route = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, route):
            raise Forbidden("Only the route's caregiver can create a trip on it.")
        if not route["approved"]:
            raise TripConflict(f"Route {route_id} must be approved before a trip can be created.")
        # DynamoDB deletes the trip 30 days after its day (contracts/dynamo-db-tables.md).
        expires = datetime.strptime(trip_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=31)
        for _ in range(5):
            trip = {
                "tripId": "T-" + secrets.token_hex(3).upper(), "routeId": route_id, "routeVersion": route["version"],
                "travellerId": route["travellerId"], "date": trip_date, "status": "planned", "startedAt": None,
                "endedAt": None, "currentStepIndex": 0, "planVersion": 1, "override": None,
                "expiresAt": int(expires.timestamp()),
            }
            try:
                self._trip_store().create(trip)
                return self._public_trip(trip)
            except TripExists:
                continue
        raise RuntimeError("could not allocate a trip id")

    def get_trip(self, trip_id: str, caller: Caller | None = None) -> dict:
        return self._public_trip(self._stored_trip(trip_id, caller))

    def list_trips(self, traveller_id: str, trip_date: str | None = None, caller: Caller | None = None) -> list[dict]:
        """A traveller's trips, newest day first. With a caller, only the ones that caller may see."""
        trips = self._trip_store().list_for_traveller(traveller_id, trip_date)
        if caller is not None:
            routes: dict = {}                                  # one route lookup per route, not per trip
            trips = [t for t in trips if self._may_see_trip(caller, t, routes)]
        return [self._public_trip(t) for t in sorted(trips, key=lambda t: (t["date"], t["tripId"]), reverse=True)]

    def _route_for_trip(self, trip: dict) -> dict:
        """The route exactly as it was when the trip was created, or a conflict if it has since changed
        (only the latest version of a route is kept, so an edited route can't be used for the old trip)."""
        try:
            route = self.get_route(trip["routeId"])
        except RouteNotFound:
            raise TripConflict(f"Route {trip['routeId']} was deleted after this trip was created. "
                               "Create a new trip on another route.") from None
        if route["version"] != trip["routeVersion"] or not route["approved"]:
            raise TripConflict(f"Route {trip['routeId']} was changed after this trip was created. "
                               "Approve the new version and create a new trip.")
        return route

    @staticmethod
    def _detector_step(step: dict) -> dict:
        """What the detectors need and nothing personal: ids, coordinates and public stop names. Instruction text
        and any names a caregiver typed stay out (contracts/dynamo-db-tables.md: detectors use IDs only)."""
        def point(p: dict) -> dict:
            return {"name": p.get("name") if p.get("code") else None, "code": p.get("code"),
                    "lat": p.get("lat"), "lng": p.get("lng")}
        cached = {"stepIndex": step["stepIndex"], "mode": step["mode"], "line": step.get("line"),
                  "from": point(step.get("from") or {}), "to": point(step.get("to") or {}),
                  "expectedDurationS": step.get("expectedDurationS"), "underground": step.get("underground")}
        if step.get("stops"):
            cached["stops"] = step["stops"]
        return cached

    @staticmethod
    def _plan_for_trip(trip: dict, route: dict) -> list[dict]:
        """The steps the traveller follows: the route's, or the plan a bridging bus or re-route changed them to."""
        return trip.get("planSteps") or route["steps"]

    def _publish_active(self, trip: dict, route: dict) -> None:
        """Idempotent, so a retry (or restarting an active trip after Redis lost its data) is safe. The trip's own
        plan, so the detectors and the disruption checker follow a bridging bus or a re-route."""
        cached = {"routeId": route["routeId"], "routeVersion": route["version"], "travellerId": route["travellerId"],
                  "steps": [self._detector_step(s) for s in self._plan_for_trip(trip, route)], "path": route["path"]}
        pipe = self._redis().pipeline()
        pipe.set(f"trip:{trip['tripId']}:route", json.dumps(cached), ex=self._s.trip_key_ttl_s)
        pipe.sadd("active-trips", trip["tripId"])
        pipe.execute()

    def start_trip(self, trip_id: str, caller: Caller | None = None) -> dict:
        trip = self._stored_trip(trip_id, caller)
        if trip["status"] == "completed":
            raise TripConflict("This trip has already finished.")
        if trip["status"] == "active":                       # already started: just make sure Redis is right
            try:
                self._publish_active(trip, self._route_for_trip(trip))
            except TripConflict:
                pass
            return self._public_trip(trip)
        self._publish_active(trip, self._route_for_trip(trip))   # Redis first: if saving fails below, a retry finishes
        started = {**trip, "status": "active", "startedAt": self._now_iso()}
        self._trip_store().replace(started, "planned")
        return self._public_trip(started)

    def end_trip(self, trip_id: str, caller: Caller | None = None) -> dict:
        trip = self._stored_trip(trip_id, caller)
        if trip["status"] == "completed":
            return self._public_trip(trip)
        if trip["status"] == "planned":
            raise TripConflict("This trip hasn't started yet.")
        pipe = self._redis().pipeline()
        pipe.srem("active-trips", trip_id)
        pipe.delete(f"trip:{trip_id}:route")
        pipe.execute()
        ended = {**trip, "status": "completed", "endedAt": self._now_iso()}
        self._trip_store().replace(ended, "active")
        return self._public_trip(ended)

    # ===== route pack: the offline file for the traveller's phone (contracts/examples/route-pack.json) =====

    def build_pack(self, trip_id: str, caller: Caller | None = None) -> dict:
        """The pack for a trip's route. Names that live in the Profiles service (traveller, caregiver) are null
        until that service exists; the app must cope with null."""
        trip = self._stored_trip(trip_id, caller)
        route = self._route_for_trip(trip)
        plan_steps = self._plan_for_trip(trip, route)
        wording = [self._pack_wording(step) for step in plan_steps]
        audio = self._audio_keys(plan_steps, wording)
        steps = []
        for step, words, audio_key in zip(plan_steps, wording, audio):
            target = step.get("to") or {}
            steps.append({
                "stepIndex": step["stepIndex"], "mode": step["mode"],
                "easyRead": words,
                "lat": target.get("lat"), "lng": target.get("lng"),     # where this step ends
                "underground": step.get("underground"),
                # audioUrl: the step read aloud (speech.py), a temporary link like the photo's; null = the phone
                # reads it with its own voice
                "photoUrl": self.photos.photo_url(step.get("photoKey")), "audioUrl": self.photos.photo_url(audio_key),
                "backup": (step.get("backup") or {}).get("instruction"),
                # What the wording tells them to look out for, with a photo of each. `sameView`: one with no photo of
                # its own says which photo shows it ("step", or another landmark's name), so the phone can say so.
                # Where it is and the spot on the route it is seen from, so the phone shows it as they get near.
                "landmarks": [{"name": m["name"], "photoUrl": self.photos.photo_url(m.get("photoKey")),
                               "sameView": m.get("sameView"), "lat": m.get("lat"), "lng": m.get("lng"),
                               "seenFrom": m.get("seenFrom")}
                              for m in step.get("landmarks") or []],
                # Every stop of a bus/train ride, first and last included, so the phone can count the stops left
                # as it passes them. Names as the wording says them; lat/lng null for a route planned before they
                # were kept (the phone then doesn't count).
                "stops": [{"name": plain(s.get("name")), "lat": s.get("lat"), "lng": s.get("lng")}
                          for s in step.get("stops") or []],
            })
        # A changed plan is a new pack version, so the phone (which polls for one) shows "Your trip has changed".
        plan = trip.get("planVersion") or 1
        return {
            "packVersion": f"{route['routeId']}-v{route['version']}" + (f"-b{plan}" if plan > 1 else ""),
            "routeId": route["routeId"],
            "routeVersion": route["version"], "travellerName": None, "steps": steps, "path": route["path"],
            "caregiver": {"name": None, "phone": None}, "messages": dict(PACK_MESSAGES),
            "generatedAt": self._now_iso(),
        }

    @staticmethod
    def _pack_wording(step: dict) -> str:
        """The words the pack shows (and reads aloud) for a step. One place, so audio made early (_speak) has the
        same key the pack looks for."""
        return step.get("easyRead") or easy_read(step)

    def _speak(self, steps: list[dict]) -> None:
        """Makes the steps' spoken audio now, at a single caregiver action (approving a route, sending a changed plan),
        so building a pack doesn't call Polly and two pods building the same new pack don't both make it.
        Best-effort: what fails here is made when the pack is built."""
        self._audio_keys(steps, [self._pack_wording(s) for s in steps])

    def _audio_keys(self, plan: list[dict], wording: list[str]) -> list[str | None]:
        """Each step's audio in the pack bucket: the step's own `audioKey` if it has one, else its wording read
        aloud (made now if it doesn't exist yet; usually it was, by _speak). None with TTS_MODE=off or when Polly
        fails."""
        own = [step.get("audioKey") for step in plan]
        if self._speech is None or self._packs is None:
            return own
        spoken = self._speech.keys([None if key else words for key, words in zip(own, wording)])
        return [key or said for key, said in zip(own, spoken)]

    def trip_pack(self, trip_id: str, caller: Caller | None = None) -> dict:
        """Builds the pack, stores it in S3 and returns a temporary link: {"packUrl", "packVersion"}."""
        pack = self.build_pack(trip_id, caller)              # problems with the trip itself are reported first
        if self._packs is None:
            raise PackUnavailable("pack storage is not configured")
        key = f"packs/{pack['routeId']}/{pack['packVersion'].split('-', 2)[-1]}.json"      # v3.json, v3-b2.json
        self._packs.put(key, json.dumps(pack).encode("utf-8"))
        return {"packUrl": self._packs.url(key), "packVersion": pack["packVersion"]}
