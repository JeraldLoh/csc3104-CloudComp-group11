"""Report a problem for a traveller, as the Aggregator (or the Heartbeat Monitor) would. For demos.

From services/alert-service, with the service running (dev.ps1, port 8005):
    python tools/send_problem.py U-1A2B3C                    # off_route at step 2
    python tools/send_problem.py U-1A2B3C no_response        # urgent
    python tools/send_problem.py U-1A2B3C missed_stop 4      # a different step = a different alert

The same traveller + problem + step again only updates the existing alert (detected 2x, ...).
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone

URL = "http://127.0.0.1:8005/internal/problems"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    traveller_id = sys.argv[1]
    problem_type = sys.argv[2] if len(sys.argv) > 2 else "off_route"
    step = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "reportId": f"demo-{now}",
        "source": "heartbeat" if problem_type == "no_response" else "aggregator",
        "travellerId": traveller_id,
        "tripId": "T-001",
        "stepIndex": step,
        "problemType": problem_type,
        "confidence": None if problem_type == "no_response" else 0.85,
        "location": {"lat": 1.4052, "lng": 103.9024},
        "batteryPct": 64,
        "timestamp": now,
    }
    request = urllib.request.Request(URL, data=json.dumps(report).encode(), headers={"Content-Type": "application/json"})
    print(urllib.request.urlopen(request).read().decode())
