"""Step photos: a Google Street View photo of where each step ends, the caregiver's own photos, and photos of the
landmarks a step's wording names. All live in the pack bucket under media/ and are handed out as presigned links.

    photos = RoutePhotos(settings, pack_store, http, station_exits)
    photos.add_street_photos(route)        on save: a photo for each step and landmark that has none (best-effort)
    photos.with_urls(route)                the route as the API returns it, with a temporary photoUrl each
    photos.store_upload(file)              a caregiver's photo, checked by its bytes -> {photoKey, photoUrl}
    photos.photograph_landmarks(route, steps)   photos for the AI's landmarks, or sameView when one would repeat

JourneyClient uses it as `client.photos`; checking who may change a route stays in the client. How a photo is found
and taken: streetview.py. `station_exits` is the client's (OneMap), passed in so a test can replace it there.
"""
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import BinaryIO, Callable

import httpx

from . import streetview
from .config import Settings
from .pack_store import PackUnavailable

log = logging.getLogger("journey")


class PhotoInvalid(ValueError):
    """An uploaded step photo that isn't a JPEG, PNG or WebP image, or is too big."""


class RoutePhotos:
    # Step photos and landmarks and their Street View photos
    #
    # A step's `photoKey` is an S3 key in the pack bucket: a Street View photo (media/streetview/..., filled in on
    # save for steps without one) or the caregiver's upload (media/uploads/..., kept like caregiver-written
    # easyRead). Responses carry `photoUrl`, a presigned link made per response and never stored.
    #
    # A step's `landmarks` (from "Simplify with AI", landmarks.py) are the buildings its wording tells the traveller
    # to look out for, each with its own Street View photo, taken from the street facing it. They are kept only
    # while the wording still names them, so a landmark the caregiver edits out of the text goes with it.

    def __init__(self, settings: Settings, packs, http: httpx.Client,
                 station_exits: Callable[[str], list[tuple[float, float]]]):
        """`packs`: the pack bucket (pack_store.S3PackStorage), or None: then there are no photos."""
        self._s = settings
        self._packs = packs
        self._http = http
        self._station_exits = station_exits

    def add_street_photos(self, data: dict) -> dict:
        """Street View photos for steps with no photoKey (under rough/ when only an old panorama was there), and a
        recent one for a step with a rough photo once Google has one; and for landmarks without one. Best-effort:
        a step stays without one when Street View has no panorama there at all, Google or S3 fails, or photos are
        off (no API key or no bucket)."""
        steps = [self._stored_step(s) for s in data["steps"]]
        if not self.enabled:
            return {**data, "steps": steps}
        wanted: dict[tuple[int, int | None], tuple] = {}         # (step, landmark or None) -> (point, seen from)
        for i, step in enumerate(steps):
            key = step.get("photoKey")
            arrives = data.get("destination") if i == len(steps) - 1 else None
            point = self._photo_target(step, arrives) if not key or streetview.is_rough(key) else None
            if point:
                wanted[(i, None)] = (point, None)
            for n, mark in enumerate(step.get("landmarks") or []):
                if not mark.get("photoKey") and not mark.get("sameView"):        # sameView: shown in another photo
                    wanted[(i, n)] = self._landmark_view(mark)
        for (i, n), key in self._street_photos(wanted).items():
            if n is None:
                steps[i] = {**steps[i], "photoKey": key}
            else:
                steps[i]["landmarks"][n] = {**steps[i]["landmarks"][n], "photoKey": key}
        return {**data, "steps": steps}

    @staticmethod
    def _stored_step(step: dict) -> dict:
        """A step as it is saved: no presigned links, and only the landmarks its wording still names."""
        step = {k: v for k, v in step.items() if k != "photoUrl"}
        if "landmarks" in step:
            text = (step.get("easyRead") or "").lower()
            kept = [{k: v for k, v in m.items() if k != "photoUrl"} for m in step["landmarks"] or []
                    if m.get("name") and m["name"].lower() in text]
            step["landmarks"] = kept or None
        return step

    @property
    def enabled(self) -> bool:
        return bool(self._s.google_maps_api_key and self._packs)

    @staticmethod
    def _landmark_view(mark: dict) -> tuple:
        """(the building, the traveller's spot it is photographed from, if known)."""
        seen = mark.get("seenFrom")
        return (mark["lat"], mark["lng"]), (tuple(seen) if seen else None)

    def _street_photos(self, wanted: dict) -> dict:
        """{id: S3 key} of a Street View photo looking at each {id: (point, seen from or None)}, fetched at once;
        ids with none are left out. The same view is fetched once, here and across routes (see photo_key)."""
        if not wanted:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(wanted))) as pool:
            keys = dict(zip(wanted, pool.map(lambda view: self._street_photo(*view), wanted.values())))
        return {i: key for i, key in keys.items() if key}

    def _street_photo(self, point: tuple[float, float], near: tuple[float, float] | None = None) -> str | None:
        good, rough = streetview.photo_key(*point, near=near), streetview.photo_key(*point, rough=True, near=near)
        try:
            if self._packs.exists(good):          # the same place in another route: already fetched
                return good
            # A rough photo is looked for again on every save, so a recent one replaces it once Google has one.
            have_rough = self._packs.exists(rough)
            photo = streetview.fetch(*point, self._http, self._s.google_maps_api_key,
                                     self._s.streetview_timeout_s, self._s.streetview_max_age_years, have_rough,
                                     near)
            if photo is None:                     # Google down or refusing: the rough photo is still better
                return rough if have_rough else None
            key = rough if photo.rough else good
            if photo.image is not None:
                self._packs.put(key, photo.image, "image/jpeg")
            return key
        except PackUnavailable as e:
            log.warning("couldn't store a street view photo: %s", e)
            return None

    STATION_NAME = re.compile(r"\b(MRT|LRT) Station\b", re.IGNORECASE)

    def _photo_target(self, step: dict, destination: dict | None = None) -> tuple[float, float] | None:
        """Where a step's photo looks: the end of the step, but for a walk to a station the exit nearest to where
        the walk starts (the station's own point is underground or inside a mall).

        `destination`: the route's, given for its last step. A last walk ends there, so the photo is of the
        destination itself, not of where OneMap's leg ends (up to ~50 m off: the saved demo trips were recorded
        with rounded endpoints). A destination that is a station ("Orchard MRT Station (NS22)") gets its exit."""
        start = step.get("from") or {}
        station = streetview.walks_to_station(step)
        end = step.get("to") or {}
        if (destination and destination.get("lat") is not None and destination.get("lng") is not None
                and step.get("mode") == "walk" and not end.get("code")):
            if not self.STATION_NAME.search(destination.get("name") or ""):
                return destination["lat"], destination["lng"]
            station = destination
        if station and start.get("lat") is not None and start.get("lng") is not None:
            exit_point = streetview.nearest(self._station_exits(station["name"]), (start["lat"], start["lng"]))
            if exit_point:
                return exit_point
            if station is destination:
                return destination["lat"], destination["lng"]
        return streetview.target(step)

    def photo_url(self, key: str | None) -> str | None:
        return self._packs.url(key, self._s.photo_url_ttl_s) if key and self._packs else None

    def with_urls(self, route: dict) -> dict:
        """The route as the API returns it: each step, and each of its landmarks, with a temporary `photoUrl` for
        its photo (or null)."""
        return {**route, "steps": [self.step_with_urls(s) for s in route["steps"]]}

    def step_with_urls(self, step: dict) -> dict:
        out = {**step, "photoUrl": self.photo_url(step.get("photoKey"))}
        if step.get("landmarks"):
            out["landmarks"] = [{**m, "photoUrl": self.photo_url(m.get("photoKey"))} for m in step["landmarks"]]
        return out

    # Magic bytes, so the file's own claim about its type isn't trusted. WebP is RIFF....WEBP, checked below.
    PHOTO_TYPES = ((b"\xff\xd8\xff", "jpg", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "png", "image/png"))

    def store_upload(self, photo: bytes | BinaryIO) -> dict:
        """Checks a caregiver's photo (by its bytes, not its name) and stores it: {"photoKey", "photoUrl"}. Reads only
        one byte past the size limit. Raises PhotoInvalid, or PackUnavailable without a bucket."""
        data = photo if isinstance(photo, bytes) else photo.read(self._s.photo_max_bytes + 1)
        if len(data) > self._s.photo_max_bytes:
            raise PhotoInvalid(f"The photo is too big. The limit is {self._s.photo_max_bytes // (1024 * 1024)} MB.")
        found = next(((ext, ctype) for magic, ext, ctype in self.PHOTO_TYPES if data.startswith(magic)), None)
        if found is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            found = ("webp", "image/webp")
        if found is None:
            raise PhotoInvalid("The file isn't a photo. Use a JPEG, PNG or WebP image.")
        if self._packs is None:
            raise PackUnavailable("photo storage is not configured")
        key = f"media/uploads/{hashlib.sha256(data).hexdigest()[:16]}.{found[0]}"
        self._packs.put(key, data, found[1])
        return {"photoKey": key, "photoUrl": self.photo_url(key)}

    def photograph_landmarks(self, route: dict, steps: list[dict]) -> None:
        """A photo of each landmark in `steps` (the AI's answer), unless it would look (nearly) the same as the
        step's own photo or an earlier landmark's, e.g. a mall next to the bus stop, seen from the stop. Such a
        landmark gets no photo of its own; `sameView` says where it is seen instead ("step", or the other
        landmark's name). Which panorama and direction each photo would use is free to find out (metadata)."""
        by_index = {s["stepIndex"]: s for s in route["steps"]}
        last = route["steps"][-1]["stepIndex"] if route["steps"] else None
        views: dict[tuple[int, int | None], tuple] = {}            # (step, landmark or None) -> (point, seen from)
        for i, s in enumerate(steps):
            step = by_index.get(s["stepIndex"])
            if not s["landmarks"] or step is None:
                continue
            key = step.get("photoKey")
            target = None if key and not key.startswith("media/streetview/") else self._photo_target(
                step, route.get("destination") if s["stepIndex"] == last else None)     # an upload can't be compared
            if target:
                views[(i, None)] = (target, None)
            for n, m in enumerate(s["landmarks"]):
                views[(i, n)] = self._landmark_view(m)
        with ThreadPoolExecutor(max_workers=min(8, len(views) or 1)) as pool:
            seen_as = dict(zip(views, pool.map(lambda v: streetview.view(
                *v[0], self._http, self._s.google_maps_api_key, self._s.streetview_timeout_s,
                self._s.streetview_max_age_years, near=v[1]), views.values())))
        wanted = {}
        for i, s in enumerate(steps):
            shown = [("step", seen_as[(i, None)])] if seen_as.get((i, None)) else []
            for n, m in enumerate(s["landmarks"]):
                mine = seen_as.get((i, n))
                same = next((label for label, other in shown if mine and streetview.same_view(mine, other)), None)
                if same:
                    m["sameView"] = same
                    continue
                if mine:
                    shown.append((m["name"], mine))
                wanted[(i, n)] = views[(i, n)]
        for (i, n), key in self._street_photos(wanted).items():
            steps[i]["landmarks"][n]["photoKey"] = key
