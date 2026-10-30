"""Step photos: Street View on save (best-effort), caregiver uploads, and presigned links in answers and packs."""
import dataclasses
import io
from datetime import date

import fakeredis
import httpx
import pytest
import respx
from pydantic import ValidationError

from app.journey_client import JourneyClient, PhotoInvalid
from app.models import Step
from app.route_store import RouteNotFound
from app.streetview import (IMAGE_URL, METADATA_URL, bearing, distance_m, fetch, field_of_view, nearest, photo_key,
                            target, walks_to_station)
from helpers import SETTINGS, route_data

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100


class FakeBucket:
    """The pack store's interface, in memory."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}

    def ping(self):
        pass

    def put(self, key, body, content_type="application/json"):
        self.objects[key] = (body, content_type)

    def exists(self, key):
        return key in self.objects

    def url(self, key, ttl_s=None):
        return f"https://bucket.test/{key}?ttl={ttl_s}"


def client(key="g-key", bucket=None):
    s = dataclasses.replace(SETTINGS, google_maps_api_key=key)
    return JourneyClient(s, redis_client=fakeredis.FakeRedis(), pack_store=bucket, photo_http=httpx.Client())


def pano(pano_id, lat, lng, by="© 2026 Google", taken=None):
    taken = taken or f"{date.today().year - 1}-03"                                # last year: recent enough
    return {"status": "OK", "pano_id": pano_id, "location": {"lat": lat, "lng": lng}, "copyright": by, "date": taken}


def image_ok():
    return respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))


def street_view_ok():
    """A Google panorama right where each question is asked."""
    respx.get(METADATA_URL).mock(side_effect=lambda request: httpx.Response(
        200, json=pano(request.url.params["location"], *map(float, request.url.params["location"].split(",")))))
    return image_ok()


def asked_at(call) -> tuple[float, float]:
    lat, lng = call.request.url.params["location"].split(",")
    return float(lat), float(lng)


def test_bearing():
    assert round(bearing((1.0, 103.0), (1.1, 103.0))) == 0          # due north
    assert round(bearing((1.0, 103.0), (1.0, 103.1))) == 90         # due east


def test_distance_nearest_and_station_walks():
    assert round(distance_m((1.0, 103.0), (1.001, 103.0))) == 111
    assert nearest([(1.0, 103.0), (1.002, 103.0)], (1.0019, 103.0)) == (1.002, 103.0)
    assert nearest([], (1.0, 103.0)) is None
    station = {"name": "Sengkang MRT Station", "code": "NE16", "lat": 1.3917, "lng": 103.8955}
    assert walks_to_station({"mode": "walk", "to": station}) == station
    assert walks_to_station({"mode": "walk", "to": {**station, "code": "67009"}}) is None    # a bus stop
    assert walks_to_station({"mode": "train", "to": station}) is None


def test_change_of_line_inside_a_station_gets_no_street_photo():
    ne6 = {"name": "Dhoby Ghaut MRT Station", "code": "NE6", "lat": 1.29962, "lng": 103.84538}
    ns24 = {"name": "Dhoby Ghaut MRT Station", "code": "NS24", "lat": 1.29869, "lng": 103.84612}
    change = {"mode": "walk", "from": ne6, "to": ns24}
    assert target(change) is None and walks_to_station(change) is None
    from_bus = {"mode": "walk", "from": {**ne6, "code": "08057"}, "to": ns24}
    assert walks_to_station(from_bus) == ns24 and target(from_bus) == (1.29869, 103.84612)


@pytest.mark.parametrize("key, ok", [
    ("media/uploads/abc.jpg", True), ("media/streetview/v2/1.30400_103.83180.jpg", True),
    ("media/streetview/v2/rough/1.30400_103.83180.jpg", True), ("packs/R-1-v1.json", False),
    ("media/../packs/x.jpg", False), ("media/uploads/abc.exe", False)])
def test_saved_photo_keys(key, ok):
    """PUT /api/routes sends back the keys the service stored, Street View ones included (two folders deep)."""
    if ok:
        assert Step.model_validate({**route_data()["steps"][0], "photoKey": key}).photoKey == key
    else:
        with pytest.raises(ValidationError):
            Step.model_validate({**route_data()["steps"][0], "photoKey": key})


# ----- recent panoramas, Google's own first -----

STOP = (1.4052, 103.9024)


@respx.mock
def test_uploaded_panorama_is_skipped_for_a_google_one_nearby():
    def metadata(request):
        lat, lng = map(float, request.url.params["location"].split(","))
        if (lat, lng) == STOP:                                            # the nearest: someone's upload, indoors
            return httpx.Response(200, json=pano("UPLOAD", 1.40521, 103.90241, by="© aung kyaw soe win"))
        if lat > STOP[0] + 1e-4:                                         # north of the stop: a Google car went by
            return httpx.Response(200, json=pano("GOOGLE", lat, lng))
        return httpx.Response(200, json={"status": "ZERO_RESULTS"})
    respx.get(METADATA_URL).mock(side_effect=metadata)
    image = image_ok()
    assert fetch(*STOP, httpx.Client(), "g-key", 1) == (JPEG, False)
    assert image.calls[0].request.url.params["pano"] == "GOOGLE"
    assert image.calls[0].request.url.params["heading"] in {"180", "135", "225"}    # looking back at the stop


@respx.mock
def test_google_car_on_the_road_80m_away_beats_a_closer_upload():
    def metadata(request):
        lat, lng = map(float, request.url.params["location"].split(","))
        if distance_m((lat, lng), STOP) > 50:                            # the outer ring reaches the road
            return httpx.Response(200, json=pano("GOOGLE", STOP[0] + 0.00072, STOP[1]))     # 80 m north
        return httpx.Response(200, json=pano("UPLOAD", *STOP, by="© Someone"))
    meta = respx.get(METADATA_URL).mock(side_effect=metadata)
    image = image_ok()
    assert fetch(*STOP, httpx.Client(), "g-key", 1) == (JPEG, False)
    assert image.calls[0].request.url.params["pano"] == "GOOGLE"
    assert image.calls[0].request.url.params["fov"] == "45"             # zoomed in on the far-off stop
    assert meta.call_count == 17                                         # the stop, then two rings of 8 (found: no third)


@respx.mock
def test_recent_upload_at_the_stop_when_google_has_nothing_recent():
    respx.get(METADATA_URL).mock(return_value=httpx.Response(200, json=pano("UPLOAD", *STOP, by="© Someone")))
    image = image_ok()
    assert fetch(*STOP, httpx.Client(), "g-key", 1) == (JPEG, False)
    assert image.calls[0].request.url.params["pano"] == "UPLOAD"
    assert "heading" not in image.calls[0].request.url.params          # at the stop: the panorama's own view
    assert field_of_view(10) == 80 and field_of_view(60) == 53


@respx.mock
def test_far_upload_means_no_photo():
    far = pano("UPLOAD", STOP[0] + 0.0007, STOP[1], by="© Someone")                # 78 m away
    respx.get(METADATA_URL).mock(return_value=httpx.Response(200, json=far))
    image = image_ok()
    assert fetch(*STOP, httpx.Client(), "g-key", 1) is None and image.call_count == 0


@respx.mock
def test_only_old_panoramas_give_a_rough_photo():
    old = f"{date.today().year - 7}-11"                                # e.g. a walk-through of a mall years ago
    respx.get(METADATA_URL).mock(return_value=httpx.Response(200, json=pano("INDOOR", *STOP, taken=old)))
    image_ok()
    assert fetch(*STOP, httpx.Client(), "g-key", 1) == (JPEG, True)
    assert fetch(*STOP, httpx.Client(), "g-key", 1, max_age_years=10) == (JPEG, False)   # STREETVIEW_MAX_AGE_YEARS


@respx.mock
def test_old_google_beats_a_closer_old_upload_and_is_saved_as_rough():
    old = f"{date.today().year - 8}-01"

    def metadata(request):
        lat, lng = map(float, request.url.params["location"].split(","))
        if lat > STOP[0] + 1e-4:
            return httpx.Response(200, json=pano("OLD_GOOGLE", lat, lng, taken=old))
        return httpx.Response(200, json=pano("OLD_UPLOAD", lat, lng, by="© Someone", taken=old))
    respx.get(METADATA_URL).mock(side_effect=metadata)
    image = image_ok()
    assert fetch(*STOP, httpx.Client(), "g-key", 1) == (JPEG, True)
    assert image.calls[0].request.url.params["pano"] == "OLD_GOOGLE"
    bucket = FakeBucket()
    route = client(bucket=bucket).create_route(route_data())
    keys = [s.get("photoKey") for s in route["steps"]]
    assert all(k.startswith("media/streetview/v2/rough/") for k in keys if k) and any(keys)
    assert set(keys) - {None} <= set(bucket.objects)


@respx.mock
def test_refused_key_stops_after_one_question():
    meta = respx.get(METADATA_URL).mock(return_value=httpx.Response(200, json={
        "status": "REQUEST_DENIED", "error_message": "This API is not activated"}))
    assert fetch(*STOP, httpx.Client(), "g-key", 1) is None
    assert meta.call_count == 1                                          # no probing around a refusal


# ----- a walk to a station is photographed at its exit -----

ONEMAP_SEARCH = f"{SETTINGS.onemap_base_url}/api/common/elastic/search"
SENGKANG = {"name": "Sengkang MRT Station", "code": "NE16", "lat": 1.3916935, "lng": 103.8954844}
EXITS = [("SENGKANG MRT STATION EXIT A", 1.39183, 103.89541), ("SENGKANG MRT STATION EXIT C", 1.39088, 103.89521),
         ("SENGKANG MRT STATION EXIT D", 1.39244, 103.89574), ("SENGKANG GENERAL HOSPITAL", 1.3955, 103.8930)]


def station_walk() -> dict:
    data = route_data()
    walk = data["steps"][0]
    data["steps"] = [{**walk, "from": {"name": "Home", "code": None, "lat": 1.3900, "lng": 103.8950},     # south
                      "to": SENGKANG}]
    return data


@respx.mock
def test_walk_to_a_station_photographs_the_exit_on_the_travellers_side():
    search = respx.get(ONEMAP_SEARCH).mock(return_value=httpx.Response(200, json={
        "found": 4, "totalNumPages": 1,
        "results": [{"SEARCHVAL": n, "LATITUDE": str(lat), "LONGITUDE": str(lng)} for n, lat, lng in EXITS]}))
    meta = respx.get(METADATA_URL).mock(return_value=httpx.Response(200, json=pano("P1", 1.39080, 103.89515)))
    image_ok()
    j = client(bucket=FakeBucket())
    route = j.create_route(station_walk())
    assert route["steps"][0]["photoKey"] == photo_key(1.39088, 103.89521)          # exit C, the southern one
    assert asked_at(meta.calls[0]) == (1.39088, 103.89521)
    assert search.calls[0].request.url.params["searchVal"] == "SENGKANG MRT STATION EXIT"
    j.create_route(station_walk())
    assert search.call_count == 1                                                    # exits are cached


@respx.mock
def test_exits_unknown_falls_back_to_the_station_point():
    respx.get(ONEMAP_SEARCH).mock(side_effect=httpx.ConnectError("down"))
    meta = respx.get(METADATA_URL).mock(return_value=httpx.Response(200, json=pano("P1", 1.39170, 103.89548)))
    image_ok()
    route = client(bucket=FakeBucket()).create_route(station_walk())
    assert route["steps"][0]["photoKey"] == photo_key(SENGKANG["lat"], SENGKANG["lng"])
    assert asked_at(meta.calls[0]) == (SENGKANG["lat"], SENGKANG["lng"])


@respx.mock
def test_save_adds_street_view_photos_and_answers_carry_links():
    image = street_view_ok()
    bucket = FakeBucket()
    j = client(bucket=bucket)
    route = j.create_route(route_data())
    keys = [s.get("photoKey") for s in route["steps"]]
    assert all(k and k.startswith("media/streetview/") for k in keys)
    assert all(bucket.objects[k][1] == "image/jpeg" for k in keys)
    assert "key=g-key" in str(image.calls[0].request.url)              # sent to Google, never stored or returned
    shown = j.with_photo_urls(route)
    assert shown["steps"][0]["photoUrl"] == f"https://bucket.test/{keys[0]}?ttl={SETTINGS.photo_url_ttl_s}"
    assert "photoUrl" not in j.get_route(route["routeId"])["steps"][0]    # never stored


@respx.mock
def test_same_stop_is_fetched_once_and_uploads_are_kept():
    image = street_view_ok()
    bucket = FakeBucket()
    j = client(bucket=bucket)
    data = route_data()
    first = data["steps"][0]["to"]
    bucket.put(photo_key(first["lat"], first["lng"]), JPEG, "image/jpeg")         # already in S3
    data["steps"][1]["photoKey"] = "media/uploads/abc.jpg"                        # the caregiver's photo
    route = j.create_route(data)
    assert route["steps"][1]["photoKey"] == "media/uploads/abc.jpg"
    assert image.call_count == 0


@respx.mock
def test_no_panorama_or_google_down_still_saves():
    respx.get(METADATA_URL).mock(return_value=httpx.Response(200, json={"status": "ZERO_RESULTS"}))
    assert all(s.get("photoKey") is None for s in client(bucket=FakeBucket()).create_route(route_data())["steps"])
    respx.get(METADATA_URL).mock(side_effect=httpx.ConnectError("down"))
    assert all(s.get("photoKey") is None for s in client(bucket=FakeBucket()).create_route(route_data())["steps"])


def test_photos_off_without_key_or_bucket():
    for j in (client(key="", bucket=FakeBucket()), client(bucket=None)):
        assert all(s.get("photoKey") is None for s in j.create_route(route_data())["steps"])


def test_upload_checks_the_bytes_not_the_name():
    bucket = FakeBucket()
    j = client(key="", bucket=bucket)
    rid = j.create_route(route_data())["routeId"]
    out = j.upload_photo(rid, JPEG)
    assert out["photoKey"].startswith("media/uploads/") and out["photoKey"].endswith(".jpg")
    assert bucket.objects[out["photoKey"]][1] == "image/jpeg"
    with pytest.raises(PhotoInvalid):
        j.upload_photo(rid, b"just some text")
    with pytest.raises(PhotoInvalid):
        j.upload_photo(rid, JPEG + b"0" * SETTINGS.photo_max_bytes)


# ----- rough photos are looked for again; exits and uploads -----

def echo(taken):
    """A Google panorama right where each question is asked, taken `taken` ("2018-01")."""
    return lambda request: httpx.Response(200, json=pano("P", *map(float, request.url.params["location"].split(",")),
                                                          taken=taken))


@respx.mock
def test_rough_photo_is_replaced_once_google_has_a_recent_one():
    bucket = FakeBucket()
    j = client(bucket=bucket)
    meta = respx.get(METADATA_URL).mock(side_effect=echo(f"{date.today().year - 8}-01"))
    image = image_ok()
    first = j.create_route(route_data())
    assert all(s["photoKey"].startswith("media/streetview/v2/rough/") for s in first["steps"] if s.get("photoKey"))
    downloads = image.call_count

    again = j.create_route({**route_data(), "steps": first["steps"]})              # still only old ones there
    assert [s.get("photoKey") for s in again["steps"]] == [s.get("photoKey") for s in first["steps"]]
    assert image.call_count == downloads                                            # no paid download again

    meta.side_effect = httpx.ConnectError("down")                                   # Google down: keep the rough one
    assert j.create_route({**route_data(), "steps": first["steps"]})["steps"] == again["steps"]

    meta.side_effect = echo(f"{date.today().year - 1}-01")                          # the street car came by
    later = j.create_route({**route_data(), "steps": first["steps"]})
    keys = [s.get("photoKey") for s in later["steps"] if s.get("photoKey")]
    assert keys and not any("/rough/" in k for k in keys) and set(keys) <= set(bucket.objects)


@respx.mock
def test_no_exits_found_is_only_remembered_for_an_hour():
    respx.get(ONEMAP_SEARCH).mock(return_value=httpx.Response(200, json={"found": 0, "totalNumPages": 1, "results": []}))
    respx.get(METADATA_URL).mock(return_value=httpx.Response(200, json={"status": "ZERO_RESULTS"}))
    j = client(bucket=FakeBucket())
    assert j.station_exits("Sengkang MRT Station (NE16)") == []
    assert 0 < j._r.ttl("journey:cache:station-exits:SENGKANG MRT STATION") <= JourneyClient.NO_EXITS_TTL_S


def test_upload_is_not_read_before_the_route_is_checked():
    class Unreadable:
        def read(self, n):
            raise AssertionError("read before the route was checked")
    with pytest.raises(RouteNotFound):
        client(key="", bucket=FakeBucket()).upload_photo("R-000000", Unreadable())
    j = client(key="", bucket=FakeBucket())
    rid = j.create_route(route_data())["routeId"]
    assert j.upload_photo(rid, io.BytesIO(JPEG))["photoKey"].endswith(".jpg")
