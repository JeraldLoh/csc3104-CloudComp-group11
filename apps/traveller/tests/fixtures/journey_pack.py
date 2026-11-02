"""Makes journey-pack.json: a route pack built by the real Journey Service code (Dennis's build_pack), so the
app's tests check the pack the phone actually receives, not a hand-written copy of the contract.

    pip install -r services/journey-service/requirements-dev.txt
    python apps/traveller/tests/fixtures/journey_pack.py

Run it again whenever the Journey Service changes what goes in a pack. The route is the journey-service tests'
sample route (Punggol, walk + bus 190) with AI-style wording and landmarks: one with its own photo per step, and
one that has no photo because it would look the same as the step photo (sameView "step").
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT / "services" / "journey-service"), str(ROOT / "services" / "journey-service" / "tests")]

import fakeredis  # noqa: E402
from helpers import NOW, SETTINGS, route_data  # noqa: E402
from datetime import timezone  # noqa: E402
from app.journey_client import JourneyClient  # noqa: E402


class Bucket:
    """The pack store's interface, in memory (as in the journey-service tests). Links look like S3 signed links."""
    def ping(self):
        pass

    def put(self, key, body, content_type="application/json"):
        pass

    def exists(self, key):
        return True

    def url(self, key, ttl_s=None):
        return f"https://careroute-files.s3.ap-southeast-1.amazonaws.com/{key}?X-Amz-Signature=test"


def landmark(name, lat, lng, photo_key=None, same_view=None):
    return {"name": name, "kind": "mall", "lat": lat, "lng": lng, "seenFrom": None,
            "photoKey": photo_key, "sameView": same_view}


def main() -> None:
    steps = route_data()["steps"]
    steps[0] = {**steps[0], "photoKey": "media/streetview/v2/1.40520_103.90240.jpg",
                "easyRead": "Walk to the bus stop at Punggol Interchange. Look for Waterway Point on your left.",
                "landmarks": [landmark("Waterway Point", 1.4064, 103.9022,
                                       "media/streetview/v2/1.40640_103.90220_from_1.40450_103.90180.jpg")]}
    steps[1] = {**steps[1], "photoKey": "media/streetview/v2/1.39350_103.89620.jpg",
                "easyRead": "Wait for bus number 190. Tap your card. Count 6 stops. "
                            "Look out for Oasis Terraces on your right. Press the bell. Get off at Block 221.",
                "landmarks": [landmark("Oasis Terraces", 1.4026, 103.9128,
                                       "media/streetview/v2/1.40260_103.91280_from_1.40100_103.91000.jpg"),
                              landmark("Block 221", 1.3936, 103.8963, same_view="step")]}

    client = JourneyClient(SETTINGS, redis_client=fakeredis.FakeRedis(), pack_store=Bucket(),
                           sleep=lambda _: None, clock=lambda: NOW.astimezone(timezone.utc))
    route = client.approve_route(client.create_route(route_data(steps=steps))["routeId"])
    pack = client.build_pack(client.create_trip(route["routeId"], "2026-10-22")["tripId"])

    out = Path(__file__).with_name("journey-pack.json")
    out.write_text(json.dumps(pack, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
