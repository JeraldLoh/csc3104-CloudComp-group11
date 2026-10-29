"""Put a trip's route and context in Redis, standing in for the Journey Service at trip start.

The real detectors read trip:{id}:route and :context (contracts/redis-keys.md). Uses the
contract's example route, which the example check-in sits on.
"""
import json
import os

import redis

TRIP = os.getenv("TRIP", "T-001")
TRAVELLER = os.getenv("TRAVELLER", "U-042")

route = json.load(open("/tools/route.json"))
r = redis.Redis.from_url(os.environ["REDIS_URL"])
r.set(f"trip:{TRIP}:route", json.dumps({"steps": route["steps"], "path": route["path"]}), ex=86400)
r.hset(f"trip:{TRIP}:context",
       mapping={"travellerId": TRAVELLER, "stepIndex": 0, "routeVersion": route["version"], "status": "active"})
r.expire(f"trip:{TRIP}:context", 86400)
print(f"seeded route and context for {TRIP}")
