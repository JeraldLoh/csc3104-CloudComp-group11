"""One real trip through the team's services, with the traveller app in a browser. No demo mode, nothing simulated
except the phone's GPS.

  caregiver adds a traveller (Profiles, Alert Service)
  -> saves and approves a route with their own photos and landmarks, makes a trip (Journey Service)
  -> the traveller opens the trip link (traveller app): pack and photos from S3, Start my trip
  -> check-ins every 10 s (Check-in Service) -> the caregiver's live view (Alert Service)
  -> off the route: Check-in Service -> SQS -> Coordinator -> detectors -> Aggregator -> an off_route incident
  -> Help -> a help_requested incident -> I've arrived -> arrival recorded, trip ended (Journey Service)

Needs the team's local stack and the traveller app running on their usual ports, for example
`scripts/windows/local-sim.ps1` (or `scripts/macos/local-sim.sh`), which starts both. Then:

    pip install httpx playwright
    python -m playwright install chromium
    python apps/traveller/e2e/team_trip.py            # add --headed to watch, --out <folder> for screenshots

Prints one line per check and exits 1 if any required check fails. Afterwards it removes the traveller and route it
made (the trip record stays until its DynamoDB TTL); --keep leaves them for a look in the caregiver app. The off-route check needs the Coordinator,
detectors and Aggregator; without them it's reported as skipped, not failed.
"""
import argparse
import asyncio
import struct
import sys
import time
import zlib
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

p = argparse.ArgumentParser()
p.add_argument("--app", default="http://localhost:5173")
p.add_argument("--checkin", default="http://localhost:8001")
p.add_argument("--alerts", default="http://localhost:8005")
p.add_argument("--journey", default="http://localhost:8006")
p.add_argument("--caregiver", default="G-003")
p.add_argument("--out", default=None, help="folder for screenshots")
p.add_argument("--headed", action="store_true")
p.add_argument("--keep", action="store_true", help="keep the test traveller, route and trip afterwards")
args = p.parse_args()
G = {"X-Dev-User": args.caregiver}
OUT = Path(args.out) if args.out else None
results: list[tuple[str, str, str]] = []           # (PASS | FAIL | SKIP, check, detail)


def check(ok: bool | None, name: str, detail: object = "") -> None:
    status = "SKIP" if ok is None else "PASS" if ok else "FAIL"
    results.append((status, name, str(detail)[:160]))
    print(f"{status}  {name}  {str(detail)[:160]}", flush=True)


def png(rgb: tuple[int, int, int], w: int = 64, h: int = 48) -> bytes:
    """A plain-colour PNG, so the caregiver's photo uploads need no image library."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def make_trip(h: httpx.Client) -> tuple[str, str, str]:
    t = h.post(f"{args.alerts}/api/travellers", json={"name": "Alex"}, headers=G)
    t.raise_for_status()
    traveller = t.json()["travellerId"]

    stop = lambda name, code, lat, lng: {"name": name, "code": code, "lat": lat, "lng": lng}
    home, interchange, blk = (stop("Home", None, 1.404, 103.901), stop("Punggol Int", "65009", 1.4052, 103.9024),
                              stop("Opp Block 221", "65221", 1.3935, 103.8962))
    route = {
        "travellerId": traveller, "caregiverId": args.caregiver, "name": "Home to Clinic (e2e)",
        "origin": {"name": "Home", "lat": 1.404, "lng": 103.901},
        "destination": {"name": "Clinic", "lat": 1.392, "lng": 103.895},
        "steps": [
            {"stepIndex": 0, "mode": "walk", "instruction": "Walk 200 m to Punggol Int", "from": home, "to": interchange,
             "easyRead": "Walk to the bus stop at Punggol Interchange. Look for Waterway Point on your left.",
             "underground": False},
            {"stepIndex": 1, "mode": "bus", "instruction": "Take bus 190 to Opp Block 221", "from": interchange, "to": blk,
             "line": "190", "underground": False,
             "easyRead": "Wait for bus number 190. Tap your card. Count 6 stops. Look out for Oasis Terraces on your "
                         "right. Press the bell. Get off at Block 221.",
             "backup": {"instruction": "If bus 190 does not come in 20 minutes, take bus 83.", "line": "83"}},
        ],
        "path": [[1.404, 103.901], [1.4052, 103.9024], [1.3935, 103.8962], [1.392, 103.895]],
    }
    r = h.post(f"{args.journey}/api/routes", json=route, headers=G)
    r.raise_for_status()
    saved = r.json()
    rid = saved["routeId"]
    keys = {}
    for name, colour in (("step0", (90, 140, 190)), ("step1", (80, 150, 100)), ("waterway", (190, 140, 90)),
                         ("oasis", (150, 110, 180))):
        up = h.post(f"{args.journey}/api/routes/{rid}/photos", files={"photo": (f"{name}.png", png(colour), "image/png")},
                    headers=G)
        up.raise_for_status()
        keys[name] = up.json()["photoKey"]
    saved["steps"][0].update(photoKey=keys["step0"], landmarks=[
        {"name": "Waterway Point", "kind": "mall", "lat": 1.4064, "lng": 103.9022, "photoKey": keys["waterway"]}])
    saved["steps"][1].update(photoKey=keys["step1"], landmarks=[
        {"name": "Oasis Terraces", "kind": "mall", "lat": 1.4026, "lng": 103.9128, "photoKey": keys["oasis"]},
        {"name": "Block 221", "lat": 1.3936, "lng": 103.8963, "sameView": "step"}])
    body = {k: v for k, v in saved.items() if k not in ("routeId", "approved", "createdAt", "updatedAt")}
    h.put(f"{args.journey}/api/routes/{rid}", json=body, headers=G).raise_for_status()
    h.post(f"{args.journey}/api/routes/{rid}/approve", headers=G).raise_for_status()
    trip = h.post(f"{args.journey}/api/trips", json={"routeId": rid, "date": time.strftime("%Y-%m-%d")}, headers=G)
    trip.raise_for_status()
    return traveller, rid, trip.json()["tripId"]


def clean_up(h: httpx.Client, traveller: str, rid: str, trip: str) -> None:
    """Removes what make_trip() made, so runs don't pile up on the caregiver's lists. Best-effort: a failure is
    printed, never turned into a failed run."""
    try:
        if h.get(f"{args.journey}/api/trips/{trip}", headers=G).json().get("status") == "active":
            h.post(f"{args.journey}/api/trips/{trip}/end", headers=G)   # a route with a trip under way can't go
        h.delete(f"{args.journey}/api/routes/{rid}", headers=G).raise_for_status()
        h.delete(f"{args.alerts}/api/travellers/{traveller}", headers=G).raise_for_status()
        print(f"cleaned up: traveller {traveller}, route {rid}")
    except httpx.HTTPError as e:
        print(f"couldn't clean up traveller {traveller} / route {rid}: {e}")


def incidents(h: httpx.Client, traveller: str) -> list[dict]:
    return h.get(f"{args.alerts}/api/incidents", params={"travellerId": traveller}, headers=G).json()


async def shot(page, name: str) -> None:
    if OUT:
        OUT.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(OUT / f"{name}.png"))


async def main() -> int:
    h = httpx.Client(timeout=20)
    for name, url in (("Check-in Service", args.checkin), ("Alert Service", args.alerts),
                      ("Journey Service", args.journey), ("traveller app", args.app)):
        try:
            h.get(url + ("/" if name == "traveller app" else "/health")).raise_for_status()
        except httpx.HTTPError as e:
            print(f"{name} isn't answering at {url} ({e}). Start the local stack first (see this file's top).")
            return 1
    traveller, rid, trip = make_trip(h)
    try:
        return await run_trip(h, traveller, trip)
    finally:
        if not args.keep:
            clean_up(h, traveller, rid, trip)


async def run_trip(h: httpx.Client, traveller: str, trip: str) -> int:
    link = f"{args.app}/?trip={trip}&traveller={traveller}&debug=1"
    print(f"trip link: {link}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                        geolocation={"latitude": 1.4045, "longitude": 103.9018},
                                        permissions=["geolocation"])
        page = await ctx.new_page()
        await page.goto(link)
        try:
            await page.get_by_text("Your trip is saved").wait_for(timeout=30000)
            check(True, "traveller app opened the real trip: pack and photos downloaded")
        except Exception:
            check(False, "traveller app opened the real trip", "no 'Your trip is saved' after 30 s: is LocalStack "
                  "reachable on localhost:4566 (pack links)?")
            await shot(page, "fail-open")
            await browser.close()
            return 1
        await shot(page, "1-start")

        await page.get_by_role("button", name="Start my trip").click()
        await page.wait_for_timeout(3000)
        status = h.get(f"{args.journey}/api/trips/{trip}", headers=G).json().get("status")
        check(status == "active", "Journey Service: trip started by the phone", status)

        await ctx.set_geolocation({"latitude": 1.4052, "longitude": 103.9024})
        await page.get_by_role("button", name="I've done this step").click()
        await page.wait_for_timeout(12000)
        live = (h.get(f"{args.alerts}/api/travellers/live", headers=G).json() or {}).get(traveller) or {}
        check(live.get("tripId") == trip and live.get("stepIndex") == 1, "caregiver's live view: trip, step 2, location",
              {k: live.get(k) for k in ("tripId", "stepIndex", "lat", "lng")})
        imgs = await page.eval_on_selector_all("img", "els => els.map(e => [e.className, e.naturalWidth > 0])")
        check(["sign__landmark-photo", True] in imgs and ["sign__photo", True] in imgs,
              "step 2 shows the step photo and the landmark photo", imgs)
        captions = await page.locator("figcaption, .sign__landmark-note").all_inner_texts()
        check(captions[:1] == ["Look for Oasis Terraces"] and any("Block 221" in c for c in captions),
              "landmark lines", captions)
        await shot(page, "2-bus")

        # Off the route, on purpose: only real check-ins from this phone, through the detection chain.
        for lat, lng in [(1.3990, 103.9150), (1.3985, 103.9165), (1.3980, 103.9180), (1.3975, 103.9195), (1.3970, 103.9210)]:
            await ctx.set_geolocation({"latitude": lat, "longitude": lng})
            await page.wait_for_timeout(10500)
        found = [i for i in incidents(h, traveller) if i.get("problemType") == "off_route"]
        check(True if found else None, "detectors: going off the route made an off_route incident",
              found[0].get("confidence") if found else "none: are the Coordinator, detectors and Aggregator running?")
        await ctx.set_geolocation({"latitude": 1.3935, "longitude": 103.8962})

        await page.get_by_role("button", name="Help").click()
        await page.wait_for_timeout(3000)
        helped = [i for i in incidents(h, traveller) if i.get("problemType") == "help_requested"]
        check(bool(helped), "Help: a help_requested incident for the caregiver", helped[0].get("incidentId") if helped else "")
        await shot(page, "3-help")
        await page.get_by_role("button", name="I'm OK now").click()

        await page.get_by_role("button", name="I've arrived").click()
        await page.wait_for_timeout(4000)
        live = (h.get(f"{args.alerts}/api/travellers/live", headers=G).json() or {}).get(traveller) or {}
        check(bool(live.get("arrivedAt")), "I've arrived: the caregiver is told", live.get("arrivedAt"))
        status = h.get(f"{args.journey}/api/trips/{trip}", headers=G).json().get("status")
        check(status == "completed", "Journey Service: trip ended by the phone", status)
        await shot(page, "4-arrived")
        await browser.close()

    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed or skipped, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
