"""Put one check-in on careroute-checkins, standing in for the Check-in Service.

Starts from contracts/examples/checkin.json (on route, inside the fake detectors' area).

    send --seq 1                      on route
    send --seq 2 --off-route          off route (outside the fake detectors' area)
    send --seq 3 --lat 1.45 --lng 103.95
"""
import argparse
import json
import os
from datetime import datetime, timezone

import boto3

ON_ROUTE = (1.4052, 103.9024)
OFF_ROUTE = (1.4500, 103.9500)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seq", type=int, required=True)
    p.add_argument("--trip", default="T-001")
    p.add_argument("--traveller", default="U-042")
    p.add_argument("--step", type=int, default=2)
    p.add_argument("--off-route", action="store_true")
    p.add_argument("--lat", type=float)
    p.add_argument("--lng", type=float)
    p.add_argument("--queue", default="careroute-checkins")
    a = p.parse_args()

    lat, lng = OFF_ROUTE if a.off_route else ON_ROUTE
    checkin = {
        "tripId": a.trip, "travellerId": a.traveller, "seq": a.seq,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lat": a.lat if a.lat is not None else lat, "lng": a.lng if a.lng is not None else lng,
        "accuracyM": 12, "speedMps": 1.3, "headingDeg": 85, "stepIndex": a.step, "routeVersion": 3,
        "batteryPct": 64, "status": "active", "bufferedOffline": False,
    }
    # Like the Check-in Service: the real detectors read these keys. Only when REDIS_URL is set.
    if os.getenv("REDIS_URL"):
        import redis

        r = redis.Redis.from_url(os.environ["REDIS_URL"])
        r.lpush(f"trip:{a.trip}:recent", json.dumps(checkin))
        r.ltrim(f"trip:{a.trip}:recent", 0, 9)
        r.hset(f"trip:{a.trip}:context", mapping={
            "travellerId": a.traveller, "stepIndex": a.step, "routeVersion": 3, "status": "active"})
    sqs = boto3.client("sqs")
    url = sqs.get_queue_url(QueueName=a.queue)["QueueUrl"]
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps(checkin))
    print(f"sent {a.trip}#{a.seq} at ({checkin['lat']}, {checkin['lng']}) to {a.queue}")


if __name__ == "__main__":
    main()
