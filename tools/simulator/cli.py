# command line for generate replay and load

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional

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

    args = parser.parse_args(argv)
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
