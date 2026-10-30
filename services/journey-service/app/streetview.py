"""Landmark photos: a Google Street View picture of where a step ends, looking at it.

    target(step)                   -> (lat, lng) to photograph, or None (train steps, changes of line, no coordinates)
    walks_to_station(step)         -> the MRT/LRT station a walk step ends at, or None
    nearest(points, to)            -> the point closest to `to` (e.g. the station exit on the traveller's side)
    photo_key(lat, lng, rough)     -> "media/streetview/v2/[rough/]<lat>_<lng>.jpg", the S3 key (same place = same photo)
    fetch(lat, lng, http, key, t)  -> Photo(image, rough), or None (no panorama nearby, or any error)

A step ends where the traveller must recognise something: the bus stop to wait at, the stop to get off at, the
destination. That is also the point the route pack gives for the step (`lat`/`lng`). Train steps are skipped: a
street view above an underground platform would mislead; the walk after the train shows the station exit.
A station's own point is the middle of the station (underground, or inside a mall), so a walk to a station is
photographed at the station exit nearest to where the walk starts (journey_client.py looks the exits up).

Recent panoramas are used first (see _recent), and Google's own before uploads (see panorama): photos people upload
to Street View also count as "outdoor" but are often indoors (a mall corridor). An upload is used only when no recent
Google panorama is within MAX_DISTANCE_M, and only if it is close to the stop. With nothing recent at all, the
closest older panorama is still used, as a rough photo the caregiver is asked to check or replace. The camera is
then turned from the panorama towards the stop, and zoomed in the further away it is, so the stop is in the middle
of the picture.

The API key is never logged (httpx errors carry the URL, so only their type is, and httpx's own request log is
turned down to warnings). A refusal from Google (REQUEST_DENIED: the API isn't enabled or the key is wrong) is
logged with Google's message.
"""
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import NamedTuple

import httpx

from .geo import distance_m

log = logging.getLogger("journey")
# httpx logs every request URL at INFO, and Street View takes the API key in the URL. Warnings still show.
logging.getLogger("httpx").setLevel(logging.WARNING)

METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
IMAGE_URL = "https://maps.googleapis.com/maps/api/streetview"
SEARCH_RADIUS_M = 50                     # how far from the stop a panorama may be (and an upload, at most)
PROBE_RINGS_M = (30, 60, 90)             # no Google panorama there: also look this far away, in 8 directions
PROBE_RADIUS_M = 30
MAX_DISTANCE_M = 100                     # a Google panorama further from the stop than this doesn't show it
MAX_AGE_YEARS = 5                        # default for STREETVIEW_MAX_AGE_YEARS
AT_STOP_M = 5                            # a panorama this close is at the stop: no heading towards it


def _is_station(point: dict) -> bool:
    """It has a station code (NE16, CC13); bus stop codes are all digits."""
    code = str(point.get("code") or "")
    return bool(code) and not code.isdigit()


def changes_line(step: dict) -> bool:
    """A walk from one station platform to another (Dhoby Ghaut NE6 -> NS24): it stays inside the station, so a
    street photo would send the traveller outside. Like a train step, it gets none (the app asks for one)."""
    return step.get("mode") == "walk" and _is_station(step.get("from") or {}) and _is_station(step.get("to") or {})


def target(step: dict) -> tuple[float, float] | None:
    end = step.get("to") or {}
    if step.get("mode") == "train" or changes_line(step) or end.get("lat") is None or end.get("lng") is None:
        return None
    return end["lat"], end["lng"]


def walks_to_station(step: dict) -> dict | None:
    """The station a walk step ends at from outside it (not a change of line inside one)."""
    end = step.get("to") or {}
    if step.get("mode") == "walk" and end.get("name") and _is_station(end) and not changes_line(step):
        return end
    return None


def nearest(points: list[tuple[float, float]], to: tuple[float, float]) -> tuple[float, float] | None:
    return min(points, key=lambda p: distance_m(p, to), default=None)


def photo_key(lat: float, lng: float, rough: bool = False) -> str:
    # v2: recent panoramas, Google's first. Photos stored before (which may be old uploads) aren't reused for new
    # saves; routes that already point at one keep it. A rough photo (only an old panorama there) is kept under
    # rough/, which is how the app knows to ask the caregiver to check it (isRoughPhoto in apps/caregiver).
    return f"media/streetview/v2/{'rough/' if rough else ''}{lat:.5f}_{lng:.5f}.jpg"


def is_rough(key: str) -> bool:
    return key.startswith("media/streetview/v2/rough/")


def bearing(frm: tuple[float, float], to: tuple[float, float]) -> float:
    """Compass bearing in degrees (0 = north) from one point to another."""
    lat1, lat2 = math.radians(frm[0]), math.radians(to[0])
    dlng = math.radians(to[1] - frm[1])
    x = math.sin(dlng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


class _Refused(Exception):
    """Google refused the request (REQUEST_DENIED etc.): asking again from other points won't help."""


def _by_google(m: dict) -> bool:
    """Google's own ("© 2026 Google"); photos people upload carry the person's name."""
    return "google" in (m.get("copyright") or "").lower()


def _recent(m: dict, max_age_years: int) -> bool:
    """Taken in the last `max_age_years`. Google's indoor walk-throughs (malls, stations, galleries) also say
    Google, but in Singapore they are old (2014, 2019) while the street car came by in 2022-2025, so the age
    limit keeps them out."""
    try:
        year, month = (int(x) for x in str(m.get("date") or "").split("-")[:2])
    except ValueError:
        return False                                             # no date: can't tell, so don't trust it
    today = date.today()
    return (today.year - year) * 12 + (today.month - month) <= max_age_years * 12


def _metadata(lat: float, lng: float, radius: int, http: httpx.Client, api_key: str, timeout: float) -> dict | None:
    meta = http.get(METADATA_URL, timeout=timeout, params={
        "location": f"{lat},{lng}", "source": "outdoor", "radius": radius, "key": api_key})
    meta.raise_for_status()
    m = meta.json()
    if m.get("status") == "OK":
        return m
    if m.get("status") != "ZERO_RESULTS":         # ZERO_RESULTS: no panorama here. Anything else: key or project
        raise _Refused(f"{m.get('status')} {m.get('error_message', '')}")
    return None


def _around(lat: float, lng: float, metres: float) -> list[tuple[float, float]]:
    """8 points `metres` away from (lat, lng): N, NE, E, ..."""
    out = []
    for deg in range(0, 360, 45):
        a = math.radians(deg)
        out.append((lat + metres * math.cos(a) / 111_320,
                    lng + metres * math.sin(a) / (111_320 * math.cos(math.radians(lat)))))
    return out


def _away(m: dict, frm: tuple[float, float]) -> float:
    return distance_m((m["location"]["lat"], m["location"]["lng"]), frm)


def panorama(lat: float, lng: float, http: httpx.Client, api_key: str, timeout: float,
             max_age_years: int = MAX_AGE_YEARS) -> tuple[dict, bool] | None:
    """(metadata, rough) of the panorama to photograph (lat, lng) from, or None if there is none at all:
      1. the recent Google panorama closest to it, within MAX_DISTANCE_M;
      2. else the closest recent upload within SEARCH_RADIUS_M;
      3. else, rough: the closest older one (Google's within MAX_DISTANCE_M first, then any within SEARCH_RADIUS_M).
         It may be years old or indoors, so the app asks the caregiver to check it.

    The metadata call only gives the one panorama nearest the point asked about, and next to a station or a stop
    that is nearly always an upload. Google's street car stays on the road, often 60-90 m away, so rings of
    points around the stop are asked too (the call is free) until a recent Google panorama turns up. A ring's 8
    points are asked at once, so a slow Google costs at most 4 timeouts, not 25."""
    here = (lat, lng)
    found: dict[str, dict] = {}

    def look(points: list[tuple[float, float]], radius: int) -> None:
        with ThreadPoolExecutor(max_workers=len(points)) as pool:
            for m in pool.map(lambda p: _metadata(*p, radius, http, api_key, timeout), points):
                if m:
                    found[m["pano_id"]] = m

    look([here], SEARCH_RADIUS_M)
    for ring in PROBE_RINGS_M:
        if any(_by_google(m) and _recent(m, max_age_years) for m in found.values()):
            break
        look(_around(lat, lng, ring), PROBE_RADIUS_M)

    def closest(google: bool, recent: bool) -> dict | None:
        within = MAX_DISTANCE_M if google else SEARCH_RADIUS_M
        return min((m for m in found.values() if _by_google(m) == google and _recent(m, max_age_years) == recent
                    and _away(m, here) <= within), key=lambda m: _away(m, here), default=None)

    if m := closest(google=True, recent=True) or closest(google=False, recent=True):
        return m, False
    if m := closest(google=True, recent=False) or closest(google=False, recent=False):
        return m, True
    return None


def field_of_view(metres: float) -> int:
    """Zoom in on a stop seen from further away, so it isn't a speck: 80 degrees up to 40 m, down to 45."""
    return round(max(45, min(80, 3200 / max(metres, 1))))


class Photo(NamedTuple):
    image: bytes | None                               # None: rough, and the caller already has a rough photo
    rough: bool                                       # nothing recent there: an older one, for the caregiver to check


def fetch(lat: float, lng: float, http: httpx.Client, api_key: str, timeout: float,
          max_age_years: int = MAX_AGE_YEARS, have_rough: bool = False) -> Photo | None:
    """`have_rough`: a rough photo of this spot is already stored. The search (free) still runs, in case a recent
    panorama has turned up since, but another rough image (a paid request) isn't downloaded."""
    try:
        found = panorama(lat, lng, http, api_key, timeout, max_age_years)
        if found is None:                             # no panorama at all near this stop
            log.info("no street view near %.5f,%.5f", lat, lng)
            return None
        m, rough = found
        if rough and have_rough:
            return Photo(None, True)
        if rough:
            log.info("only an old street view near %.5f,%.5f (%s)", lat, lng, m.get("date"))
        away = _away(m, (lat, lng))
        params = {"pano": m["pano_id"], "size": "640x400", "fov": field_of_view(away), "pitch": 0, "key": api_key}
        if away > AT_STOP_M:                          # at the stop itself there's no direction to it: Google's
            pano = m["location"]                      # default view of the panorama (the uploader's) is used
            params["heading"] = f"{bearing((pano['lat'], pano['lng']), (lat, lng)):.0f}"
        image = http.get(IMAGE_URL, timeout=timeout, params=params)
        image.raise_for_status()
        if not image.headers.get("content-type", "").startswith("image/"):
            return None
        return Photo(image.content, rough)
    except _Refused as e:
        log.warning("street view refused for %.5f,%.5f: %s", lat, lng, e)
        return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        log.warning("street view photo unavailable for %.5f,%.5f (%s)", lat, lng, type(e).__name__)
        return None
