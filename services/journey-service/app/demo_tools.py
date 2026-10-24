"""Demo helper: pretend LTA reports an MRT disruption (or a crowded platform), then see what the system does.

    docker exec csc3104-journey-service-1 python -m app.demo_tools on [--line NEL] [--stations NE10,NE11,NE12]
    docker exec csc3104-journey-service-1 python -m app.demo_tools check [--dry-run] [--again]
    docker exec csc3104-journey-service-1 python -m app.demo_tools off
    docker exec csc3104-journey-service-1 python -m app.demo_tools crowd on [--line NEL] [--stations NE17] [--level high]
    docker exec csc3104-journey-service-1 python -m app.demo_tools crowd off

How it works: the LTA client keeps LTA's train-alert response in Redis (`journey:cache:train-alerts`) for a minute
and the planner and the disruption checker both read it from there. `on` writes a pretend "major disruption"
response into that key (for `--minutes`, default 30), so:
  * planning a route over those stations shows a red `train_disruption` warning on its MRT step, and
  * `check` (the disruption Lambda's job) sends a disruption message for every active trip on those stations.
`off` removes it and the real LTA feed is used again. `crowd on` does the same for platform crowding: it writes a
pretend LTA response into the cache key the planner reads (`platform-crowd:<line>`), so a route boarding at those
stations shows a `crowded` warning; `crowd off` removes every pretend crowd. Only runs when DEMO_UI=true, so it can't be used by accident
on a real deployment (and it is not part of the Lambda package).
"""
import argparse
import json
import sys
from datetime import datetime, timedelta

import redis

from .config import Settings
from .disruption import DisruptionChecker
from .lta import SST, LtaClient

CACHE_KEY = "journey:cache:train-alerts"          # JsonCache prefix + the key LtaClient.train_alerts() uses
CROWD_KEY = "journey:cache:platform-crowd:{line}"                  # what LtaClient.platform_crowd() caches
CROWD_LEVELS = {"low": "l", "moderate": "m", "high": "h"}
# The whole North East Line (NE2 doesn't exist): the default, so any trip along it is affected.
NEL_STATIONS = ",".join(f"NE{n}" for n in range(1, 18) if n != 2)


def pretend_response(line: str, stations: str, now: datetime) -> dict:
    """What LTA's TrainServiceAlerts returns during a major disruption (contracts: Status 2 = disrupted)."""
    return {
        "odata.metadata": "https://datamall2.mytransport.sg/ltaodataservice/$metadata#TrainServicesAlerts",
        "value": {
            "Status": 2,
            "AffectedSegments": [{"Line": line, "Direction": "Both", "Stations": stations, "FreePublicBus": "",
                                  "FreeMRTShuttle": "", "MRTShuttleDirection": ""}],
            "Message": [{"Content": f"DEMO: no train service on the {line} between {stations.split(',')[0]} and "
                                    f"{stations.split(',')[-1]} due to a signalling fault.",
                         "CreatedDate": now.astimezone(SST).strftime("%Y-%m-%d %H:%M:%S")}],
        },
    }


def simulate(r, line: str, stations: str, minutes: int, now: datetime | None = None) -> dict:
    response = pretend_response(line, stations, now or datetime.now(SST))
    r.set(CACHE_KEY, json.dumps(response), ex=minutes * 60)
    return response


def clear(r) -> bool:
    return bool(r.delete(CACHE_KEY))


def simulate_crowd(r, line: str, stations: str, level: str, minutes: int, now: datetime | None = None) -> dict:
    """Pretend LTA's PCDRealTime says `level` ("low"|"moderate"|"high") at these stations (the others unknown)."""
    now = (now or datetime.now(SST)).astimezone(SST)
    start = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
    response = {"value": [{"Station": s, "StartTime": start.isoformat(), "EndTime": (start + timedelta(minutes=10)).isoformat(),
                           "CrowdLevel": CROWD_LEVELS[level]} for s in stations.split(",")]}
    r.set(CROWD_KEY.format(line=line), json.dumps(response), ex=minutes * 60)
    return response


def clear_crowd(r) -> int:
    return sum(bool(r.delete(k)) for k in r.scan_iter(CROWD_KEY.format(line="*")))


def run_check(r, settings: Settings, send, again: bool = False, dry_run: bool = False) -> dict:
    """What the Lambda does: one pass over the active trips. `again` reports a disruption even if it was already
    sent (otherwise a repeat run only counts duplicates). `dry_run` leaves no trace, so a real run still sends."""
    return DisruptionChecker(r, LtaClient(settings, redis_client=r), settings).check(
        send, dry_run=dry_run, ignore_sent=again)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.demo_tools", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    on = sub.add_parser("on", help="pretend LTA reports an MRT disruption")
    on.add_argument("--line", default="NEL", help="LTA line code, e.g. NEL, NSL, EWL, CCL, DTL, TEL (default NEL)")
    on.add_argument("--stations", default=NEL_STATIONS, help="affected station codes, comma separated (default: the whole NEL)")
    on.add_argument("--minutes", type=int, default=30, help="how long the pretend disruption lasts (default 30)")
    sub.add_parser("off", help="go back to the real LTA feed")
    crowd = sub.add_parser("crowd", help="pretend a platform is crowded (or remove it)")
    crowd.add_argument("action", choices=("on", "off"))
    crowd.add_argument("--line", default="NEL", help="LTA TrainLine, e.g. NEL, NSL, EWL (default NEL)")
    crowd.add_argument("--stations", default="NE17", help="station codes, comma separated (default NE17, Punggol)")
    crowd.add_argument("--level", choices=tuple(CROWD_LEVELS), default="high")
    crowd.add_argument("--minutes", type=int, default=30, help="how long it lasts (default 30)")
    check = sub.add_parser("check", help="run the disruption checker once, as the Lambda would")
    check.add_argument("--dry-run", action="store_true", help="show the messages without sending them to the queue")
    check.add_argument("--again", action="store_true", help="report disruptions already sent before")
    args = parser.parse_args(argv)

    s = Settings()
    if not s.demo_ui:
        print("Refusing to run: this is a demo tool and DEMO_UI is not true.", file=sys.stderr)
        return 2
    r = redis.Redis.from_url(s.redis_url, decode_responses=True)

    if args.command == "on":
        stations = ",".join(x.strip().upper() for x in args.stations.split(",") if x.strip())
        simulate(r, args.line.upper(), stations, args.minutes)
        print(f"Pretending {args.line.upper()} is disrupted at {stations} for {args.minutes} min. "
              "Plan a route over those stations to see the warning, or run `check`.")
    elif args.command == "crowd":
        if args.action == "off":
            print(f"Removed {clear_crowd(r)} pretend crowd(s); using the real platform crowding again.")
        else:
            stations = ",".join(x.strip().upper() for x in args.stations.split(",") if x.strip())
            simulate_crowd(r, args.line.upper(), stations, args.level, args.minutes)
            print(f"Pretending {stations} on the {args.line.upper()} has {args.level} platform crowding for {args.minutes} min. "
                  "Plan a route that boards a train there to see the warning.")
    elif args.command == "off":
        print("Back to the real LTA feed." if clear(r) else "There was no pretend disruption (already using the real feed).")
    else:
        sent = []

        def send(event: dict) -> None:
            if not args.dry_run:
                import boto3
                from . import aws_resources
                sqs = boto3.client("sqs", region_name=s.aws_region, endpoint_url=s.aws_endpoint_url)
                url = aws_resources.ensure_disruptions_queue(sqs, s.disruptions_queue)
                sqs.send_message(QueueUrl=url, MessageBody=json.dumps(event))
            sent.append(event)

        summary = run_check(r, s, send, again=args.again, dry_run=args.dry_run)
        print(("Would send" if args.dry_run else "Sent") + f" {len(sent)} message(s). Summary: {summary}")
        for event in sent:
            print(json.dumps(event, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
