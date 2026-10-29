"""Prometheus metrics at GET /metrics (the standard format for Kubernetes monitoring).

Used for the evaluation (issue 2.5) and to watch the service while it runs.
"""
from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter("checkin_http_requests_total", "HTTP requests to /api", ["path", "status"])
REQUEST_SECONDS = Histogram(
    "checkin_http_request_seconds", "Time to answer an /api request", ["path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
CHECKINS = Counter("checkin_checkins_total", "Check-ins by outcome", ["outcome"])  # accepted/recovered/duplicate
DELIVERY_LAG = Histogram(
    "checkin_delivery_lag_seconds",
    "Phone timestamp to server arrival. live = sent straight away; buffered = held on the phone while offline",
    ["kind"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800),
)
IN_FLIGHT = Gauge("checkin_in_flight_requests", "/api requests being handled right now")
SHED = Counter("checkin_shed_total", "Requests turned away with 503 because this replica was at capacity")
RECONCILED = Counter("checkin_reconciled_total", "Stuck check-ins finished by the clean-up task")
LEADER = Gauge("checkin_reconciler_leader", "1 if this replica currently runs the clean-up task")
