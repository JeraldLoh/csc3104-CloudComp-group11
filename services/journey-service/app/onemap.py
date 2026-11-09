"""OneMap, Singapore's map API: places, MRT station exits, buildings and public-transport routes.

    onemap = OneMapClient(settings, redis_client=redis)
    onemap.search_places("Tampines Mall")         [{name, address, postalCode, lat, lng}]
    onemap.route_pt(start, end, when, mode)       OneMap's raw itineraries (planning.py turns them into steps)
    onemap.station_exits("Sengkang MRT Station")  [(lat, lng)] of its exits (step photos)
    onemap.nearby_buildings(lat, lng)             [{name, lat, lng}] (landmarks for the AI wording)

Like LtaClient (lta.py) for LTA: JourneyClient uses it as `client.onemap`. Requests that need a token log in with the
OneMap account (onemap_token.py) and log in again once if OneMap refuses the token early. In saved-data mode
(replay.py) search and routing answer from app/saved_data; station exits and buildings always ask live OneMap
(fixed public data, needed for the Street View photos, which are live too). Failures raise OneMapError subclasses;
the exits and buildings lookups are best-effort and return [] instead.
"""
import logging
import re
import time
from datetime import datetime, timezone
from typing import Callable

import httpx

from .cache import JsonCache
from .config import Settings
from .onemap_token import OneMapToken

log = logging.getLogger("journey")


class OneMapError(Exception):
    pass


class OneMapUnavailable(OneMapError):
    """OneMap is down, slow or rate-limiting us."""


class OneMapAuthError(OneMapError):
    """The token is missing, expired or rejected."""


def titlecase(name: str | None) -> str | None:
    """'PUNGGOL MRT STATION' -> 'Punggol MRT Station'."""
    if not name:
        return None
    return re.sub(r"\b(Mrt|Lrt)\b", lambda m: m.group(1).upper(), name.title())


def pretty(name: str | None) -> str | None:
    """Like titlecase, but OneMap's 'Origin'/'Destination' placeholders become None."""
    return None if name in ("Origin", "Destination") else titlecase(name)


def clean(value: str | None) -> str | None:
    """OneMap fills missing fields with 'NIL' or ''."""
    value = (value or "").strip()
    return None if value.upper() in ("", "NIL") else value


class OneMapClient:
    STATION_EXITS_TTL_S = 7 * 24 * 3600   # exits don't move: cached a week, so they still come back when OneMap is down
    NO_EXITS_TTL_S = 3600                 # a station with no exits found: cached an hour (OneMap may be incomplete)
    BUILDINGS_TTL_S = 30 * 24 * 3600      # buildings don't move
    BUILDINGS_RADIUS_M = 100

    def __init__(self, settings: Settings, redis_client=None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), http: httpx.Client | None = None,
                 mode=None):
        """`http` given (tests): always used, the data mode is ignored. `mode`: data_mode.DataMode, read on every
        call so a live/saved switch takes effect without a restart; None = ONEMAP_MODE, fixed."""
        self._s = settings
        self._sleep = sleep
        self._cache = JsonCache(redis_client)
        self._mode = mode
        self._fixed_live = http is not None
        self._http = http or httpx.Client(base_url=settings.onemap_base_url, timeout=settings.onemap_timeout_s)
        self._recorded_http: httpx.Client | None = None   # built on first use in saved-data mode
        # Logs in with the OneMap account when there is one, and only when live data is asked for.
        self._token = OneMapToken(settings, redis_client, clock=clock)

    # ----- transport -----

    @property
    def recorded(self) -> bool:
        """Answering from saved responses (replay.py) instead of the internet."""
        if self._fixed_live:
            return False
        return (self._mode.get("onemap") if self._mode else self._s.onemap_mode) == "recorded"

    def _client(self, recorded: bool) -> httpx.Client:
        if not recorded:
            return self._http
        if self._recorded_http is None:
            from .replay import RecordedOneMap       # only loaded in saved-data mode (see replay.py)
            self._recorded_http = httpx.Client(base_url=self._s.onemap_base_url, transport=RecordedOneMap())
        return self._recorded_http

    def get(self, path: str, params: dict, auth: bool = True) -> dict:
        recorded = self.recorded                     # read once, so one request never mixes modes
        http = self._client(recorded)
        headers = {}
        if auth and not recorded:
            token = self._token.get() or self._s.onemap_api_key          # the account's token, else the pasted one
            if not token:
                raise OneMapAuthError("No OneMap token: set ONEMAP_API_EMAIL and ONEMAP_API_PASSWORD, or ONEMAP_API_KEY")
            headers["Authorization"] = f"Bearer {token}"
        last: Exception | None = None
        renewed = False
        for attempt in range(self._s.onemap_retries + 1):
            if attempt:
                self._sleep(0.5 * 2 ** (attempt - 1))
            try:
                r = http.get(path, params=params, headers=headers)
            except httpx.HTTPError as e:
                last = e
                continue
            if r.status_code in (401, 403) and auth and not recorded and self._token.enabled and not renewed:
                renewed = True                       # it expired (or was revoked) early: log in again, once
                fresh = self._token.get(refresh=True)
                if fresh:
                    headers["Authorization"] = f"Bearer {fresh}"
                    try:
                        r = http.get(path, params=params, headers=headers)
                    except httpx.HTTPError as e:
                        last = e
                        continue
            if r.status_code in (401, 403):
                raise OneMapAuthError(f"OneMap rejected the token ({r.status_code}); it may have expired")
            if r.status_code == 429 or r.status_code >= 500:
                last = OneMapUnavailable(f"OneMap returned {r.status_code}")
                continue
            if r.status_code >= 400:
                raise OneMapError(f"OneMap returned {r.status_code} for {path}")
            try:
                return r.json()
            except ValueError as e:
                last = e
        log.warning("OneMap %s failed after %d attempts: %s", path, self._s.onemap_retries + 1, type(last).__name__)
        raise OneMapUnavailable("OneMap is not responding") from last

    # ----- what the service asks for -----

    def search_places(self, query: str, limit: int = 10) -> list[dict]:
        """Place/address search: [{name, address, postalCode, lat, lng}]. Needs no token."""
        key = f"{'recorded:' if self.recorded else ''}places:{query.strip().lower()}"   # saved and live never mix
        results = self._cache.cached(key, self._s.places_ttl_s, lambda: (
            self.get("/api/common/elastic/search", {
                "searchVal": query, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": "1"},
                auth=False).get("results") or []))
        places = []
        for r in results:
            try:
                lat, lng = float(r["LATITUDE"]), float(r["LONGITUDE"])
            except (KeyError, TypeError, ValueError):
                continue
            places.append({
                "name": titlecase(clean(r.get("BUILDING")) or clean(r.get("SEARCHVAL")) or clean(r.get("ADDRESS"))),
                "address": titlecase(clean(r.get("ADDRESS"))),
                "postalCode": clean(r.get("POSTAL")),
                "lat": lat,
                "lng": lng,
            })
        return places[:limit]

    def station_exits(self, station_name: str) -> list[tuple[float, float]]:
        """Where an MRT/LRT station's exits are, from OneMap's search ("SENGKANG MRT STATION EXIT A"); [] if unknown.
        Exits are fixed public data and the search needs no token, so this always asks live OneMap, also in
        saved-data mode (like the Street View photos it is for). Best-effort: a failure means no exits."""
        name = re.sub(r"\s*\(.*?\)", "", station_name).strip().upper()     # "Sengkang MRT Station (NE16)"

        def look_up() -> list[list[float]]:
            exits = []
            for page in (1, 2, 3):                       # 10 results a page; big interchanges have more exits
                r = self._http.get("/api/common/elastic/search", params={
                    "searchVal": f"{name} EXIT", "returnGeom": "Y", "getAddrDetails": "N", "pageNum": str(page)})
                r.raise_for_status()
                found = r.json()
                exits += [[float(x["LATITUDE"]), float(x["LONGITUDE"])] for x in found.get("results") or []
                          if (x.get("SEARCHVAL") or "").upper().startswith(f"{name} EXIT ")]
                if page >= int(found.get("totalNumPages") or 1):
                    break
            return exits

        try:
            return [(lat, lng) for lat, lng in self._cache.cached(f"station-exits:{name}", self.STATION_EXITS_TTL_S,
                                                                  look_up, empty_ttl_s=self.NO_EXITS_TTL_S)]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("couldn't look up the exits of %s (%s)", name, type(e).__name__)
            return []

    def nearby_buildings(self, lat: float, lng: float) -> list[dict]:
        """Named buildings within BUILDINGS_RADIUS_M of a point, [{name, lat, lng}], from OneMap's reverse geocode
        (for landmarks.py). Like station_exits: fixed public data, so always live OneMap, also in saved-data mode,
        and best-effort: no token or a failure means none. Cached on a ~10 m grid."""
        def look_up() -> list[dict]:
            token = self._token.get() or self._s.onemap_api_key
            if not token:
                return []
            r = self._http.get("/api/public/revgeocode", headers={"Authorization": f"Bearer {token}"}, params={
                "location": f"{lat},{lng}", "buffer": str(self.BUILDINGS_RADIUS_M), "addressType": "All",
                "otherFeatures": "N"})
            r.raise_for_status()
            out = []
            for b in r.json().get("GeocodeInfo") or []:
                name = clean(re.sub(r"\s*\(.*?\)", "", b.get("BUILDINGNAME") or ""))   # "HAWKER CENTRE (BLK 208B ...)"
                if name:
                    out.append({"name": titlecase(name), "lat": float(b["LATITUDE"]), "lng": float(b["LONGITUDE"])})
            return out

        try:
            return self._cache.cached(f"buildings:{lat:.4f}_{lng:.4f}", self.BUILDINGS_TTL_S, look_up,
                                      empty_ttl_s=self.NO_EXITS_TTL_S)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("couldn't look up buildings near %.4f,%.4f (%s)", lat, lng, type(e).__name__)
            return []

    def route_pt(self, start: tuple[float, float], end: tuple[float, float], when: datetime,
                 mode: str = "TRANSIT", max_walk_m: int | None = None, num_itineraries: int | None = None) -> dict:
        """OneMap's raw public-transport itineraries: plan.itineraries[].legs[] (see app/saved_data/fixtures)."""
        return self.get("/api/public/routingsvc/route", {
            "start": f"{start[0]},{start[1]}",
            "end": f"{end[0]},{end[1]}",
            "routeType": "pt",
            "date": when.strftime("%m-%d-%Y"),                # OneMap wants MM-DD-YYYY
            "time": when.strftime("%H:%M:%S"),
            "mode": mode,
            "maxWalkDistance": str(max_walk_m or self._s.plan_max_walk_m),
            "numItineraries": str(num_itineraries or self._s.plan_options),
        })
