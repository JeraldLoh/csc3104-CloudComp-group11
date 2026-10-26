# seed redis with one mock trip per scenario or print one request with --request SCENARIO

import argparse
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
ROUTE = json.loads((FIXTURES / "route.json").read_text())


# name and the 10 check-ins ending at each scenario interesting moment
def scenarios():
    for path in sorted(FIXTURES.glob("*-browser.json")):
        rows = json.loads(path.read_text())
        marked = [i for i, r in enumerate(rows) if r["expected"] != "normal"]
        moment = marked[-1] if marked else len(rows) // 2
        yield path.stem.removesuffix("-browser"), rows[max(0, moment - 9) : moment + 1]


# detector request for the last check-in of a window
def request_body(window):
    last = {k: v for k, v in window[-1].items() if k not in ("scenario", "expected")}
    return {"requestId": f"{last['tripId']}#{last['seq']}", "riskLevel": "normal", "checkin": last}


# seed redis or print one request body
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--redis", default="redis://localhost:6379/0")
    ap.add_argument("--request", metavar="SCENARIO")
    args = ap.parse_args()

    if args.request:
        window = dict(scenarios()).get(args.request)
        if window is None:
            ap.error(f"unknown scenario; choose from {[n for n, _ in scenarios()]}")
        print(json.dumps(request_body(window)))
        return

    import redis

    r = redis.Redis.from_url(args.redis, decode_responses=True)
    route = json.dumps({"steps": ROUTE["steps"], "path": ROUTE["path"]})
    for name, window in scenarios():
        trip = window[-1]["tripId"]
        r.set(f"trip:{trip}:route", route)
        r.delete(f"trip:{trip}:recent")
        r.hset(
            f"trip:{trip}:context",
            mapping={k: window[-1][k] for k in ("travellerId", "stepIndex", "routeVersion", "status")}
        )
        for c in window:  # oldest first so LPUSH leaves the newest at the head
            r.lpush(f"trip:{trip}:recent", json.dumps({k: v for k, v in c.items() if k not in ("scenario", "expected")}))
        print(f"seeded {trip} ({name}, expected {window[-1]['expected']})")


if __name__ == "__main__":
    main()
