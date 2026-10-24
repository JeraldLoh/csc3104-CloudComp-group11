"""Capture API data for tests, the offline demo, or both.

Run from services/journey-service:

    python scripts/capture_data.py --target tests
    python scripts/capture_data.py --target demo --routes fixtures
    python scripts/capture_data.py --target both --routes live

The tests target writes raw API responses to tests/fixtures. The demo target
builds the connected saved-data dataset under app/demo_data. API keys are read
from the environment or the repository .env file and are never written.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
OUT = ROOT / "app" / "demo_data"
SST = timezone(timedelta(hours=8))
sys.path.insert(0, str(ROOT))
from app.lta import LINE_CODES  # noqa: E402

def place(search: str, pick: str, label: str | None = None, at: tuple[float, float] | None = None) -> dict:
    """An MRT station found by OneMap place search: `search` is the query, `pick` text that must be in the result
    (picks the right station), `label` the name shown on the demo page. `at` pins the coordinates OneMap routes
    from/to; without it the station's own coordinates are used."""
    return {"search": search, "pick": pick, "label": label or search, "at": at}


# Each trip is recorded for "bus or train" and "bus only" ("train only" is filtered out of the first when replayed).
# Only the first two have saved OneMap captures in tests/fixtures (--routes fixtures); the rest need --routes live
# with a valid OneMap token.
TRIPS = [
    {"id": "punggol-orchard", "name": "Punggol to Orchard",                       # north-east to the city
     "from": place("Punggol MRT Station", "(NE17)", at=(1.4043, 103.9021)),
     "to": place("Orchard MRT Station", "(NS22)", at=(1.3040, 103.8318)),
     "fixtures": {"TRANSIT": "pt_bus_mrt", "BUS": "pt_bus_only"}},
    {"id": "amk-toapayoh", "name": "Ang Mo Kio to Toa Payoh",                    # north
     "from": place("Ang Mo Kio MRT Station", "(NS16)", at=(1.3700, 103.8486)),
     "to": place("Toa Payoh MRT Station", "(NS19)", at=(1.3326, 103.8474)),
     "fixtures": {"TRANSIT": "pt_bus_short", "BUS": "pt_bus_only_short"}},
    # north-east
    {"id": "sengkang-serangoon", "name": "Sengkang to Serangoon",
     "from": place("Sengkang MRT Station", "(NE16)"), "to": place("Serangoon MRT Station", "(NE12)")},
    {"id": "hougang-dhobyghaut", "name": "Hougang to Dhoby Ghaut",
     "from": place("Hougang MRT Station", "(NE14)"), "to": place("Dhoby Ghaut MRT Station", "(NS24)")},
    # east
    {"id": "tampines-changi", "name": "Tampines to Changi Airport",
     "from": place("Tampines MRT Station", "(EW2)"), "to": place("Changi Airport MRT Station", "(CG2)")},
    {"id": "pasirris-bugis", "name": "Pasir Ris to Bugis",
     "from": place("Pasir Ris MRT Station", "(EW1)"), "to": place("Bugis MRT Station", "(EW12)")},
    {"id": "bedok-payalebar", "name": "Bedok to Paya Lebar",
     "from": place("Bedok MRT Station", "(EW5)"), "to": place("Paya Lebar MRT Station", "(EW8)")},
    # south
    {"id": "harbourfront-marinabay", "name": "HarbourFront to Marina Bay",
     "from": place("HarbourFront MRT Station", "(NE1 / CC29)", label="HarbourFront MRT Station"),
     "to": place("Marina Bay MRT Station", "(NS27)")},
    {"id": "tiongbahru-payalebar", "name": "Tiong Bahru to Paya Lebar",
     "from": place("Tiong Bahru MRT Station", "(EW17)"), "to": place("Paya Lebar MRT Station", "(EW8)")},
]
MODES = ("TRANSIT", "BUS")


def load_env() -> dict[str, str]:
    values = dict(os.environ)
    env_file = ROOT.parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return values


def onemap_token(env: dict[str, str]) -> str:
    """A OneMap token: logged in fresh with ONEMAP_API_EMAIL / ONEMAP_API_PASSWORD when they are set (tokens last
    3 days), else the pasted ONEMAP_API_KEY. Never printed."""
    email, password = env.get("ONEMAP_API_EMAIL"), env.get("ONEMAP_API_PASSWORD")
    if email and password:
        base = env.get("ONEMAP_BASE_URL", "https://www.onemap.gov.sg").rstrip("/")
        response = httpx.post(f"{base}/api/auth/post/getToken", json={"email": email, "password": password}, timeout=30)
        if response.status_code == 200 and response.json().get("access_token"):
            return response.json()["access_token"]
        print(f"OneMap login failed (HTTP {response.status_code}); trying ONEMAP_API_KEY instead")
    token = env.get("ONEMAP_API_KEY")
    if not token:
        raise SystemExit("Set ONEMAP_API_EMAIL and ONEMAP_API_PASSWORD (or ONEMAP_API_KEY) in .env first.")
    return token


def fetch_lta(env: dict[str, str], stop: str, only: str | None) -> None:
    key = env.get("LTA_API_KEY")
    if not key:
        raise SystemExit("Set LTA_API_KEY first.")

    calls = {
        "lta_bus_arrival.json": ("v3/BusArrival", {"BusStopCode": stop}),
        "lta_bus_stops_page1.json": ("BusStops", {}),
        "lta_train_alerts.json": ("TrainServiceAlerts", {}),
        "lta_traffic_incidents.json": ("TrafficIncidents", {}),
        "lta_platform_crowd_nel.json": ("PCDRealTime", {"TrainLine": "NEL"}),
        "lta_platform_forecast_nel.json": ("PCDForecast", {"TrainLine": "NEL"}),
    }
    if only:
        calls = {name: call for name, call in calls.items() if only in name}

    FIXTURES.mkdir(parents=True, exist_ok=True)
    base = env.get("LTA_BASE_URL", "https://datamall2.mytransport.sg/ltaodataservice")
    with httpx.Client(
        base_url=base.rstrip("/") + "/",
        timeout=10,
        headers={"AccountKey": key, "accept": "application/json"},
    ) as http:
        for name, (path, params) in calls.items():
            response = http.get(path, params=params)
            print(f"{response.status_code} {path} -> {name}")
            response.raise_for_status()
            data = response.json() if response.content else {}
            if isinstance(data.get("value"), list):
                data["value"] = data["value"][:25]
            if "forecast" in name:
                for day in data["value"]:
                    day["Stations"] = [
                        station for station in day["Stations"]
                        if station["Station"] in ("NE1", "NE17", "NE6")
                    ]
            (FIXTURES / name).write_text(json.dumps(data, indent=2), encoding="utf-8")


def fetch_onemap(env: dict[str, str]) -> None:
    token = onemap_token(env)

    trips = [
        ("pt_bus_mrt", "1.4043,103.9021", "1.3040,103.8318", "pt", {}),
        ("pt_bus_short", "1.3700,103.8486", "1.3326,103.8474", "pt", {}),
        ("pt_walk_only", "1.3000,103.8450", "1.2990,103.8470", "pt", {}),
        ("walk", "1.3000,103.8450", "1.2990,103.8540", "walk", {}),
        ("pt_bus_only", "1.4043,103.9021", "1.3040,103.8318", "pt", {"mode": "BUS"}),
        ("pt_bus_only_short", "1.3700,103.8486", "1.3326,103.8474", "pt", {"mode": "BUS"}),
    ]
    pt_params = {
        "date": "10-22-2026",
        "time": "08:00:00",
        "mode": "TRANSIT",
        "maxWalkDistance": "1000",
        "numItineraries": "3",
    }
    base = env.get("ONEMAP_BASE_URL", "https://www.onemap.gov.sg")
    FIXTURES.mkdir(parents=True, exist_ok=True)

    def parse(response: httpx.Response):
        try:
            return response.json()
        except ValueError:
            return response.text[:2000]

    def save(name: str, response: httpx.Response) -> None:
        data = {"status": response.status_code, "body": parse(response)}
        (FIXTURES / f"onemap_{name}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    with httpx.Client(base_url=base, timeout=30) as http:
        response = http.get(
            "/api/common/elastic/search",
            params={"searchVal": "Tampines Mall", "returnGeom": "Y", "getAddrDetails": "Y"},
        )
        print(f"{response.status_code} search -> onemap_search.json")
        save("search", response)

        headers = None
        for label, value in (("Bearer", f"Bearer {token}"), ("bare", token)):
            probe = http.get(
                "/api/public/routingsvc/route",
                headers={"Authorization": value},
                params={"start": trips[3][1], "end": trips[3][2], "routeType": "walk"},
            )
            print(f"auth style {label}: {probe.status_code}")
            if probe.status_code not in (401, 403):
                headers = {"Authorization": value}
                break
        if headers is None:
            save("auth_rejected", probe)
            raise SystemExit("Token rejected in both styles (expired or wrong value?).")

        for name, start, end, route_type, extra in trips:
            params = {"start": start, "end": end, "routeType": route_type}
            if route_type == "pt":
                params.update(pt_params)
            params.update(extra)
            response = http.get(
                "/api/public/routingsvc/route", headers=headers, params=params
            )
            body = parse(response)
            keys = list(body)[:8] if isinstance(body, dict) else type(body).__name__
            print(
                f"{response.status_code} route {name} -> "
                f"onemap_route_{name}.json  top-level keys: {keys}"
            )
            save(f"route_{name}", response)


def write(path: Path, data, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact else json.dumps(data, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def slim(body: dict) -> dict:
    return {key: value for key, value in body.items() if key not in ("debugOutput", "elevationMetadata")}


def point(trip: dict, end: str, coords: dict) -> tuple[float, float]:
    """Where OneMap routes from/to for this end of a trip: the pinned `at`, else the station's own coordinates."""
    spec = trip[end]
    if spec["at"]:
        return spec["at"]
    if spec["label"] not in coords:
        raise SystemExit(f"No coordinates for {spec['label']}: run without --skip-places.")
    return coords[spec["label"]]


def record_routes(source: str, env: dict[str, str], coords: dict) -> list[dict]:
    routes = []
    when = datetime.now(SST).replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    token = onemap_token(env) if source == "live" else ""
    for trip in TRIPS:
        if source == "fixtures" and "fixtures" not in trip:
            print(f"skip {trip['id']}: no saved capture in tests/fixtures (needs --routes live and a valid token)")
            continue
        start, end = point(trip, "from", coords), point(trip, "to", coords)
        for mode in MODES:
            file = f"onemap/{trip['id']}_{mode.lower()}.json"
            if source == "fixtures":
                body = json.loads((FIXTURES / f"onemap_route_{trip['fixtures'][mode]}.json").read_text(encoding="utf-8"))["body"]
            else:
                response = httpx.get(
                    "https://www.onemap.gov.sg/api/public/routingsvc/route",
                    timeout=30, headers={"Authorization": f"Bearer {token}"},
                    params={"start": f"{start[0]},{start[1]}", "end": f"{end[0]},{end[1]}",
                            "routeType": "pt", "date": when.strftime("%m-%d-%Y"),
                            "time": "08:00:00", "mode": mode, "maxWalkDistance": "1000",
                            "numItineraries": "3"},
                )
                if response.status_code != 200:
                    raise SystemExit(f"OneMap answered {response.status_code} for {trip['id']} {mode} (is the token valid?)")
                body = response.json()
                time.sleep(1)
            if not (body.get("plan") or {}).get("itineraries"):
                print(f"skip {trip['id']} {mode}: no itineraries")
                continue
            write(OUT / file, slim(body), compact=True)
            routes.append({"trip": trip["id"], "mode": mode, "file": file, "from": list(start), "to": list(end)})
            print(f"saved {file}")
    return routes


def find_places() -> dict[str, dict]:
    """OneMap's place search result for every trip end, by label. Needs no token; it rate-limits, so go slowly."""
    hits: dict[str, dict] = {}
    for trip in TRIPS:
        for end in ("from", "to"):
            spec = trip[end]
            if spec["label"] in hits:
                continue
            for attempt in range(8):
                response = httpx.get(
                    "https://www.onemap.gov.sg/api/common/elastic/search", timeout=30,
                    params={"searchVal": spec["search"], "returnGeom": "Y",
                            "getAddrDetails": "Y", "pageNum": "1"},
                )
                if response.status_code == 200:
                    break
                time.sleep(5 * (attempt + 1))
            else:
                raise SystemExit(f"OneMap place search kept answering {response.status_code}; try again in a minute.")
            hit = next((item for item in response.json().get("results", []) if spec["pick"] in item["SEARCHVAL"]), None)
            if hit is None:
                raise SystemExit(f"No place matching {spec['search']} {spec['pick']}")
            hits[spec["label"]] = hit
            print(f"found place {hit['SEARCHVAL']}")
            time.sleep(2)
    return hits


def stops_in(routes: list[dict]) -> tuple[set[str], set[str]]:
    boarding, every = set(), set()
    for route in routes:
        body = json.loads((OUT / route["file"]).read_text(encoding="utf-8"))
        for itinerary in body["plan"]["itineraries"]:
            for leg in itinerary["legs"]:
                if leg["mode"] != "BUS":
                    continue
                if leg["from"].get("stopCode"):
                    boarding.add(leg["from"]["stopCode"])
                for stop in [leg["from"], leg["to"], *(leg.get("intermediateStops") or [])]:
                    if stop.get("stopCode"):
                        every.add(stop["stopCode"])
    return boarding, every


def train_boardings(routes: list[dict]) -> dict[str, set[str]]:
    boardings: dict[str, set[str]] = {}
    for route in routes:
        body = json.loads((OUT / route["file"]).read_text(encoding="utf-8"))
        for itinerary in body["plan"]["itineraries"]:
            for leg in itinerary["legs"]:
                line = LINE_CODES.get((leg.get("route") or "").upper())
                if leg["mode"] == "SUBWAY" and line and leg["from"].get("stopCode"):
                    boardings.setdefault(line, set()).add(leg["from"]["stopCode"])
    return boardings


def record_lta(routes: list[dict], env: dict[str, str]) -> dict:
    key = env.get("LTA_API_KEY", "")
    if not key:
        raise SystemExit("Set LTA_API_KEY to record LTA data (or pass --skip-lta).")
    http = httpx.Client(
        base_url="https://datamall2.mytransport.sg/ltaodataservice/", timeout=30,
        headers={"AccountKey": key, "accept": "application/json"},
    )
    recorded_at = datetime.now(SST).isoformat(timespec="seconds")
    boarding, every = stops_in(routes)
    for old in (OUT / "lta").glob("bus_arrival_*.json"):
        old.unlink()
    saved = []
    for code in sorted(boarding):
        response = http.get("v3/BusArrival", params={"BusStopCode": code})
        if response.status_code == 200 and response.content and response.json().get("Services"):
            write(OUT / "lta" / f"bus_arrival_{code}.json", response.json())
            saved.append(code)
        time.sleep(0.3)
    stops, skip = [], 0
    while True:
        rows = http.get("BusStops", params={"$skip": skip}).json().get("value", [])
        stops += [row for row in rows if row["BusStopCode"] in every]
        if len(rows) < 500:
            break
        skip += 500
    write(OUT / "lta" / "bus_stops.json", stops)
    for old in (OUT / "lta").glob("platform_*.json"):
        old.unlink()
    boardings = train_boardings(routes)
    for line, stations in sorted(boardings.items()):
        write(OUT / "lta" / f"platform_crowd_{line}.json",
              http.get("PCDRealTime", params={"TrainLine": line}).json())
        forecast = http.get("PCDForecast", params={"TrainLine": line}).json()
        for day in forecast.get("value", []):
            day["Stations"] = [station for station in day["Stations"] if station["Station"] in stations]
        write(OUT / "lta" / f"platform_forecast_{line}.json", forecast)
    for name, path in (("train_alerts", "TrainServiceAlerts"), ("traffic_incidents", "TrafficIncidents")):
        write(OUT / "lta" / f"{name}.json", http.get(path).json())
    http.close()
    return {"recordedAt": recorded_at, "busArrivalStops": saved, "crowdLines": sorted(boardings)}


def run_demo(args: argparse.Namespace, env: dict[str, str]) -> None:
    manifest_path = OUT / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {}
    )
    hits = {} if args.skip_places else find_places()
    coords = {label: (float(hit["LATITUDE"]), float(hit["LONGITUDE"])) for label, hit in hits.items()}
    if args.routes != "skip":
        routes = record_routes(args.routes, env, coords)
        manifest["onemap"] = {
            # a date: the demo page and /demo/info show "saved on <recordedAt>"
            "recordedAt": datetime.now(SST).strftime("%Y-%m-%d") if args.routes == "live" else "2026-10-21",
            "source": "live OneMap" if args.routes == "live" else "tests/fixtures (real OneMap responses, 2026-10-21)",
        }
        manifest["routes"] = routes
    routes = manifest.get("routes", [])
    if hits:
        # Only trips that have a saved route are offered: a place without one would just answer "not saved".
        saved = [t for t in TRIPS if any(r["trip"] == t["id"] for r in routes)]
        labels = {t[end]["label"] for t in saved for end in ("from", "to")}
        write(OUT / "places.json", [hit for label, hit in hits.items() if label in labels])
        manifest["trips"] = [
            {
                "id": trip["id"],
                "name": trip["name"],
                "from": {"name": trip["from"]["label"], "lat": coords[trip["from"]["label"]][0],
                         "lng": coords[trip["from"]["label"]][1]},
                "to": {"name": trip["to"]["label"], "lat": coords[trip["to"]["label"]][0],
                       "lng": coords[trip["to"]["label"]][1]},
            }
            for trip in saved
        ]
    if not args.skip_lta:
        manifest["lta"] = record_lta(routes, env)
    write(manifest_path, manifest)
    print(f"wrote manifest.json: {len(manifest.get('trips', []))} saved trips")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("tests", "demo", "both"), default="both")
    parser.add_argument("--routes", choices=("skip", "fixtures", "live"), default="skip")
    parser.add_argument("--stop", default="83139", help="LTA bus stop used for test fixtures")
    parser.add_argument("--only", help="Only capture matching LTA fixture names")
    parser.add_argument("--skip-lta", action="store_true")
    parser.add_argument("--skip-places", action="store_true")
    args = parser.parse_args()
    env = load_env()

    if args.target in ("tests", "both"):
        fetch_lta(env, args.stop, args.only)
        fetch_onemap(env)
    if args.target in ("demo", "both"):
        run_demo(args, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
