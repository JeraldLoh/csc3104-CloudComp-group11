"""How long does a caregiver wait for a route's photos and for "Simplify with AI"?

    python services/journey-service/benchmarks/bench_latency.py
    python services/journey-service/benchmarks/bench_latency.py --routes 5 --runs 5 --force-cold

It drives the Journey Service's own API the way the caregiver app does, for a few of
the saved demo trips (app/saved_data/manifest.json), shortest to longest:

    plan      POST /api/routes/plan
    save      POST /api/routes                 Street View photos are found and stored here (photos.py)
    load      GET  /api/routes/{id}            a temporary link per photo
    photos    GET  each photoUrl               the image itself, from S3
    simplify  POST /api/routes/{id}/easy-read  landmarks -> Easy-read Service -> Ollama -> landmark photos
    (delete the route)

Each trip is done --runs times. A save is "cold" when a photo it returned was stored during that save (the image's
Last-Modified), "warm" when every photo was already in S3. The first run of a place is only cold if nobody saved a
route there before; --force-cold moves each stop a few metres on a trip's first run, so that save must fetch new
photos (each is a paid Street View image, about US$0.007).

Needs the Journey Service on --journey-url (kind local-sim and Docker Compose both use localhost:8006) with
AUTH_MODE=off. Benchmark routes belong to traveller "bench-traveller" and are deleted afterwards.
"""
import argparse
import json
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

from _common import call, finish, pearson, stop, summary, table

MANIFEST = Path(__file__).resolve().parent.parent / "app" / "saved_data" / "manifest.json"
JITTER_DEG = 0.00001                # about 1 m: a new photo key (5 decimals), the same street


def trips() -> list[dict]:
    """The saved demo trips, with the coordinates they were recorded for (so saved-data mode can answer)."""
    manifest = json.loads(MANIFEST.read_text())
    recorded = {r["trip"]: r for r in manifest["routes"] if r["mode"] == "TRANSIT"}
    return [{"id": t["id"], "name": t["name"],
             "from": {"name": t["from"]["name"], "lat": recorded[t["id"]]["from"][0], "lng": recorded[t["id"]]["from"][1]},
             "to": {"name": t["to"]["name"], "lat": recorded[t["id"]]["to"][0], "lng": recorded[t["id"]]["to"][1]}}
            for t in manifest["trips"] if t["id"] in recorded]


def spread(items: list, n: int) -> list:
    """`n` of the (sorted) items, evenly spaced from the first to the last."""
    if n >= len(items):
        return items
    if n == 1:
        return [items[len(items) // 2]]
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def moved(point: dict, by: float) -> dict:
    if point.get("lat") is None or point.get("lng") is None:
        return point
    return {**point, "lat": point["lat"] + by, "lng": point["lng"] + by}


def route_body(trip: dict, option: dict, shift: float) -> dict:
    """What the caregiver app posts to save a plan option (usePlanner.ts). `shift`: see --force-cold."""
    steps = [{**s, "to": moved(s["to"], shift)} if s["mode"] != "train" else s for s in option["steps"]]
    return {"travellerId": "bench-traveller", "caregiverId": "bench-caregiver", "name": f"Benchmark: {trip['name']}",
            "origin": trip["from"], "destination": moved(trip["to"], shift), "steps": steps, "path": option["path"]}


def photo_links(route_or_answer: dict) -> tuple[list[str], list[str]]:
    """(step photo links, landmark photo links) in a route or an easy-read answer."""
    steps = route_or_answer.get("steps") or []
    return ([s["photoUrl"] for s in steps if s.get("photoUrl")],
            [m["photoUrl"] for s in steps for m in s.get("landmarks") or [] if m.get("photoUrl")])


def download(links: list[str], since: float) -> list[dict]:
    """Each photo fetched: how long it took, its size, and whether it was stored after `since` (so: fetched fresh)."""
    out = []
    for link in links:
        r = call("GET", link, timeout=30)
        stored = None
        if r.headers.get("last-modified"):
            stored = parsedate_to_datetime(r.headers["last-modified"]).timestamp()
        out.append({"ok": r.status == 200, "ms": r.seconds * 1000, "bytes": len(r.data),
                    "fresh": stored is not None and stored >= since - 1})       # Last-Modified is in whole seconds
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--journey-url", default="http://localhost:8006")
    ap.add_argument("--routes", type=int, default=3, help="how many demo trips (default 3: shortest, middle, longest)")
    ap.add_argument("--runs", type=int, default=3, help="times each trip is saved and simplified (default 3)")
    ap.add_argument("--force-cold", action="store_true",
                    help="move each stop a few metres on a trip's first run, so it fetches new Street View photos (paid)")
    args = ap.parse_args()
    base = args.journey_url.rstrip("/")

    print("\n=== Journey Service: photo and Simplify-with-AI latency ===\n")

    if call("GET", f"{base}/health", timeout=5).status != 200:
        stop(f"no Journey Service at {base} (start the local stack: see services/journey-service/README.md)")

    # Plan every trip (fast), then keep --routes of them spread over the number of steps.
    planned = []
    for trip in trips():
        r = call("POST", f"{base}/api/routes/plan", {"from": {k: trip["from"][k] for k in ("lat", "lng")},
                                                      "to": {k: trip["to"][k] for k in ("lat", "lng")}}, timeout=60)
        if r.status == 401:
            stop("the Journey Service wants a login (AUTH_MODE): run the benchmark against a stack with AUTH_MODE=off")
        options = r.json().get("options") if r.status == 200 else None
        if options:
            planned.append({"trip": trip, "option": options[0], "planMs": r.seconds * 1000})
        else:
            print(f"  skipped {trip['id']}: plan answered {r.error or 'no options'}")
    if not planned:
        stop("no trip could be planned")
    chosen = spread(sorted(planned, key=lambda p: len(p["option"]["steps"])), args.routes)
    print(f"Trips: {', '.join(p['trip']['id'] + ' (' + str(len(p['option']['steps'])) + ' steps)' for p in chosen)}; "
          f"{args.runs} runs each\n")

    runs, warm_up_s = [], None
    for p in chosen:
        trip, option = p["trip"], p["option"]
        for n in range(1, args.runs + 1):
            # 1 to 20 m, by the clock: another invocation moves the stops somewhere else, so it is cold again.
            shift = JITTER_DEG * (1 + int(time.time()) % 20) if args.force_cold and n == 1 else 0.0
            started = time.time()
            saved = call("POST", f"{base}/api/routes", route_body(trip, option, shift), timeout=120)
            if saved.status != 201:
                print(f"  {trip['id']} run {n}: save failed ({saved.error}) {saved.data[:200]!r}")
                continue
            route = saved.json()
            route_id = route["routeId"]
            try:
                loaded = call("GET", f"{base}/api/routes/{route_id}", timeout=30)
                step_photos = download(photo_links(loaded.json() if loaded.status == 200 else route)[0], started)
                if warm_up_s is None:                   # the first call also loads the model into memory: not timed
                    warm_up_s = call("POST", f"{base}/api/routes/{route_id}/easy-read", timeout=180).seconds
                asked = time.time()
                simplified = call("POST", f"{base}/api/routes/{route_id}/easy-read", timeout=180)
                answer = simplified.json() if simplified.status == 200 else {}
                answers = answer.get("steps") or []
                landmark_photos = download(photo_links(answer)[1], asked)
            finally:
                call("DELETE", f"{base}/api/routes/{route_id}", timeout=30)
            run = {"trip": trip["id"], "run": n, "steps": len(option["steps"]), "planMs": p["planMs"],
                   "saveMs": saved.seconds * 1000, "saveCold": any(x["fresh"] for x in step_photos),
                   "loadMs": loaded.seconds * 1000, "stepPhotos": step_photos,
                   "simplifyS": simplified.seconds, "simplifyStatus": simplified.status,
                   "provider": answer.get("provider"), "fallbackSteps": sum(bool(s.get("fallback")) for s in answers),
                   "landmarks": sum(len(s.get("landmarks") or []) for s in answers), "landmarkPhotos": landmark_photos}
            runs.append(run)
            print(f"  {trip['id']} run {n}: save {run['saveMs']:.0f} ms ({'cold' if run['saveCold'] else 'warm'}), "
                  f"{len(step_photos)} photos, simplify {run['simplifyS']:.1f} s ({run['provider'] or simplified.error})")
    if not runs:
        stop("every save failed")

    # Results
    def ms(values):
        s = summary(values)
        return f"{s['p50']:.0f} / {s['p95']:.0f}" if s["n"] else "-"

    rows = []
    for p in chosen:
        mine = [r for r in runs if r["trip"] == p["trip"]["id"]]
        if not mine:
            continue
        ai = [r["simplifyS"] for r in mine if r["provider"] == "ollama"]
        rows.append([p["trip"]["id"], mine[0]["steps"], f"{p['planMs']:.0f}",
                     ms([r["saveMs"] for r in mine if r["saveCold"]]), ms([r["saveMs"] for r in mine if not r["saveCold"]]),
                     ms([r["loadMs"] for r in mine]), ms([x["ms"] for r in mine for x in r["stepPhotos"]]),
                     f"{summary(ai)['p50']:.1f} / {summary(ai)['p95']:.1f}" if ai else "-",
                     f"{sum(r['fallbackSteps'] for r in mine)}/{sum(r['steps'] for r in mine)}",
                     f"{sum(r['landmarks'] for r in mine) / len(mine):.1f}"])
    print("\nPer trip (p50 / p95):\n")
    print(table(["trip", "steps", "plan ms", "save cold ms", "save warm ms", "load ms", "photo ms", "simplify s",
                 "fallback steps", "landmarks"], rows))
    if warm_up_s is not None:
        print(f"\nFirst Simplify call (not counted: it may load the model into memory): {warm_up_s:.1f} s")

    # Overall
    ai_runs = [r for r in runs if r["provider"] == "ollama"]
    ai_s = [r["simplifyS"] for r in ai_runs]
    cold = [x["saveMs"] for x in runs if x["saveCold"]]
    warm = [x["saveMs"] for x in runs if not x["saveCold"] and x["stepPhotos"]]
    photos = [x["ms"] for r_ in runs for x in r_["stepPhotos"] + r_["landmarkPhotos"] if x["ok"]]
    loads = [x["loadMs"] for x in runs]
    r = pearson([x["steps"] for x in ai_runs], ai_s)

    def line(label: str, values: list[float], unit: str, digits: int = 0) -> list:
        s = summary(values)
        if not s["n"]:
            return [label, 0, "-", "-", "-", "-"]
        return [label, s["n"], *(f"{s[k]:.{digits}f} {unit}" for k in ("p50", "p95", "min", "max"))]

    print("\nOverall:\n")
    print(table(["measure", "n", "p50", "p95", "min", "max"], [
        line("Simplify with AI (answered by Ollama)", ai_s, "s", 1),
        line("Save route, photos fetched from Street View (cold)", cold, "ms"),
        line("Save route, photos already stored (warm)", warm, "ms"),
        line("Load route with photo links", loads, "ms"),
        line("Download one photo", photos, "ms")]))
    print(f"\nSimplify calls answered by Ollama: {len(ai_runs)} of {len(runs)} "
          f"(any others got the template wording or an error)")
    if ai_runs:
        print(f"Simplify time per step: {summary([x['simplifyS'] / x['steps'] for x in ai_runs])['p50']:.1f} s (median)"
              + (f"; correlation of steps and time: r = {r:.2f}" if r is not None else ""))
    if cold and warm:
        print(f"Cold save / warm save (medians): {summary(cold)['p50'] / summary(warm)['p50']:.1f}x")
    elif not any(x["stepPhotos"] for x in runs):
        print("No step has a photo: Street View is off (GOOGLE_MAPS_API_KEY or PACKS_BUCKET not set)")
    elif not cold:
        print("No cold save: photos of these places are already stored (use --force-cold to measure one)")

    finish("latency", {
        "settings": vars(args), "warmUpS": warm_up_s, "runs": runs,
        "summary": {"simplifyS": summary(ai_s), "stepsToTimeR": r, "saveColdMs": summary(cold),
                    "saveWarmMs": summary(warm), "loadMs": summary(loads), "photoMs": summary(photos)}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
