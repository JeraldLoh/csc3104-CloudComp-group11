# command line for generate replay load and live

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import List, Optional

from tools.simulator import live as live_mod
from tools.simulator import load as load_mod
from tools.simulator import replay as replay_mod
from tools.simulator.route import build_route
from tools.simulator.scenarios import SCENARIOS, generate

DEFAULT_OUT = "services/detectors/tests/fixtures"
REPORT_S = 10
LOCAL_DETECTORS = "http://localhost:8101,http://localhost:8102,http://localhost:8103,http://localhost:8104"


# route json with each lat lng pair on one line
def _dump_route(route: dict) -> str:
    text = json.dumps(route, indent=2)
    return re.sub(r"\[\s+(-?[\d.]+),\s+(-?[\d.]+)\s+\]", r"[\1, \2]", text) + "\n"


# one check-in per line
def _dump_checkins(checkins: list) -> str:
    return "[\n" + ",\n".join("  " + json.dumps(c) for c in checkins) + "\n]\n"


# write clean and browser fixtures per scenario plus route json
def generate_command(out: Path, seed: int, scenarios: List[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "route.json").write_text(_dump_route(build_route()))
    for name in scenarios:
        for stem, browser in ((name, False), (f"{name}-browser", True)):
            checkins = generate(name, seed, browser=browser)
            (out / f"{stem}.json").write_text(_dump_checkins(checkins))
            print(f"{stem}: {len(checkins)} check-ins -> {out / (stem + '.json')}")


# replay scenarios and print a table and summary
def replay_command(args) -> None:
    import redis  # only needed here

    r = redis.Redis.from_url(args.redis, decode_responses=True)
    urls = [u.strip() for u in args.detectors.split(",") if u.strip()]
    names = list(SCENARIOS) if args.all else [args.scenario]
    results = {}
    for name in names:
        print(f"\n== {name} ==")
        printed = []

        # print the header once then each row
        def show(row):
            if not printed:
                printed.append([max(len(replay_mod._cell(v)), len(v["detectorId"])) for v in row["votes"]])
                print(replay_mod.format_header(row, printed[0]))
            print(replay_mod.format_row(row, printed[0]), flush=True)

        rows = replay_mod.replay_scenario(name, r, urls, args.speed, args.seed, args.browser, on_row=show)
        results[name] = rows
        print(f"-> {replay_mod.write_csv(rows, name, args.results)}")
    print("\n" + replay_mod.format_summary(replay_mod.summarize(results)))
    # a dead detector only shows as error in the table so say why once
    first = {}
    for rows in results.values():
        for row in rows:
            for v in row["votes"]:
                if v["verdict"] == "error":
                    first.setdefault(v["detectorId"], v["reason"])
    for name, why in first.items():
        print(f"{name}: {why}")


# one decimal or None
def _round(ms):
    return None if ms is None else round(ms, 1)


# run the load test and print progress and a summary
def load_command(args) -> None:
    import redis  # only needed here

    r = redis.Redis.from_url(args.redis, decode_responses=True)
    r.ping()  # fail now, not after the first check-in
    urls = [u.strip() for u in args.detectors.split(",") if u.strip()]
    scenarios = args.scenario or list(SCENARIOS)
    per_detector = args.travellers / args.interval
    print(f"{args.travellers} travellers, a check-in every {args.interval:g} s each, for {args.duration:g} s")
    print(f"~{per_detector:.0f} req/s to each of {len(urls)} detectors ({per_detector * len(urls):.0f} req/s in total)")
    print(f"scenarios: {', '.join(scenarios)}\n")
    rows = []

    # print a progress line and keep it for the csv
    def show(elapsed, w):
        print(load_mod.format_window(elapsed, w, REPORT_S), flush=True)
        rows.append({
            "elapsedS": round(elapsed), "reqPerS": round(w.sent / REPORT_S, 1),
            "p50Ms": _round(load_mod.percentile(w.latencies_ms, 50)),
            "p95Ms": _round(load_mod.percentile(w.latencies_ms, 95)),
            "normal": w.verdicts.get("normal", 0), "problem": w.verdicts.get("problem", 0),
            "abstain": w.verdicts.get("abstain", 0), "error": w.verdicts.get("error", 0)
        })

    stats = load_mod.run_load(
        r, urls, args.travellers, args.interval, args.duration, scenarios,
        seed=args.seed, browser=args.browser, report_every_s=REPORT_S, on_report=show
    )
    print("\n" + load_mod.format_summary(stats, args.duration))
    if rows:
        print(f"-> {load_mod.write_csv(rows, args.results)}")


# run one scenario through the real services and list the incidents it raised
def live_command(args) -> None:
    endpoints = live_mod.Endpoints(args.journey, args.checkin, args.alert)
    run = live_mod.LiveRun(endpoints, args.caregiver, live_mod.http_call(args.caregiver))
    traveller = run.choose_traveller(args.traveller)
    version = run.start_trip()
    print(f"{args.scenario}: caregiver {args.caregiver}, traveller {traveller}, trip {run.trip_id}")
    print(f"watch it at the caregiver app acting as {args.caregiver}\n")
    try:
        sent = run.send(args.scenario, version, args.interval, args.seed, args.browser,
                        on_sent=lambda c: print(f"  #{c['seq']:>3} step {c['stepIndex']} expected {c['expected']}", flush=True))
        print(f"\n{len(sent)} check-ins sent, waiting {args.settle:g} s for the analysis to finish")
        time.sleep(args.settle)
    finally:
        if not args.keep_trip:
            run.end_trip()
    try:
        found = run.incidents()
    except live_mod.ServiceError as exc:
        print(f"can't list incidents: {exc}")
        return
    print("incidents:" if found else "incidents: none")
    for i in found:
        print(f"  {i['incidentId']}  {i.get('severity', '')}")


# parse arguments and run a command
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.simulator")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="write clean and browser-like fixtures per scenario, plus route.json")
    gen.add_argument("--out", type=Path, default=Path(DEFAULT_OUT), help="output directory")
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--scenario", action="append", choices=SCENARIOS,
                     help="only this scenario (repeatable); default is all")

    rep = sub.add_parser("replay", help="seed Redis and send a scenario's check-ins to live detectors")
    which = rep.add_mutually_exclusive_group(required=True)
    which.add_argument("--scenario", choices=SCENARIOS)
    which.add_argument("--all", action="store_true", help="run every scenario and print a summary")
    rep.add_argument("--redis", default="redis://localhost:6379/0")
    rep.add_argument("--detectors", default=LOCAL_DETECTORS)
    rep.add_argument("--speed", type=float, default=0, help="0 = as fast as possible, 1 = real time (10 s gaps)")
    rep.add_argument("--seed", type=int, default=42)
    rep.add_argument("--browser", action=argparse.BooleanOptionalAction, default=True,
                     help="browser-like check-ins (default) or clean ones")
    rep.add_argument("--results", type=Path, default=Path("results"), help="CSV output directory")

    load = sub.add_parser("load", help="many travellers checking in at once, to load-test and autoscale the detectors")
    load.add_argument("--travellers", type=int, default=50)
    load.add_argument("--interval", type=float, default=10,
                      help="seconds between one traveller's check-ins (10 = real time; smaller compresses time)")
    load.add_argument("--duration", type=float, default=300, help="seconds to run")
    load.add_argument("--scenario", action="append", choices=SCENARIOS,
                      help="scenario mix, assigned round-robin (repeatable); default is all")
    load.add_argument("--redis", default="redis://localhost:6379/0")
    load.add_argument("--detectors", default=LOCAL_DETECTORS)
    load.add_argument("--seed", type=int, default=42)
    load.add_argument("--browser", action=argparse.BooleanOptionalAction, default=True)
    load.add_argument("--results", type=Path, default=Path("results"), help="CSV output directory")

    live = sub.add_parser("live", help="send a scenario through the real services, to watch in the caregiver app")
    live.add_argument("--scenario", choices=SCENARIOS, required=True)
    live.add_argument("--caregiver", default="G-003", help="caregiver id to act as and to watch as")
    live.add_argument("--traveller", help="traveller id to use; default: the caregiver's first, or a new one")
    live.add_argument("--journey", default="http://localhost:8006", help="Journey Service address")
    live.add_argument("--checkin", default="http://localhost:8001", help="Check-in Service address, the load balancer on EKS")
    live.add_argument("--alert", default="http://localhost:8005", help="Alert Service address")
    live.add_argument("--interval", type=float, default=1.0,
                      help="seconds between sends; timestamps stay 10 s apart, so 10 = real time")
    live.add_argument("--settle", type=float, default=5.0, help="seconds to wait after the last check-in")
    live.add_argument("--keep-trip", action="store_true", help="leave the trip active instead of ending it")
    live.add_argument("--seed", type=int, help="default: a new one each run")
    live.add_argument("--browser", action=argparse.BooleanOptionalAction, default=True)

    args = parser.parse_args(argv)
    if args.command == "live":
        if args.seed is None:
            args.seed = int(time.time())
        if args.interval <= 0 or args.interval > 10:
            parser.error("--interval must be more than 0 and at most 10")
        live_command(args)
        return 0
    if args.command == "load":
        load_command(args)
        return 0
    if args.command == "replay":
        replay_command(args)
        return 0
    generate_command(args.out, args.seed, args.scenario or list(SCENARIOS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
