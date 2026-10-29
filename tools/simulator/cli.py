"""Command line: ``python -m tools.simulator generate --out services/detectors/tests/fixtures/``."""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional

from tools.simulator import replay as replay_mod
from tools.simulator.route import build_route
from tools.simulator.scenarios import SCENARIOS, generate

DEFAULT_OUT = "services/detectors/tests/fixtures"


def _dump_route(route: dict) -> str:
    # Keep each [lat, lng] pair on one line so the dense path stays readable
    text = json.dumps(route, indent=2)
    return re.sub(r"\[\s+(-?[\d.]+),\s+(-?[\d.]+)\s+\]", r"[\1, \2]", text) + "\n"


def _dump_checkins(checkins: list) -> str:
    return "[\n" + ",\n".join("  " + json.dumps(c) for c in checkins) + "\n]\n"


def generate_command(out: Path, seed: int, scenarios: List[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "route.json").write_text(_dump_route(build_route()))
    for name in scenarios:
        for stem, browser in ((name, False), (f"{name}-browser", True)):
            checkins = generate(name, seed, browser=browser)
            (out / f"{stem}.json").write_text(_dump_checkins(checkins))
            print(f"{stem}: {len(checkins)} check-ins -> {out / (stem + '.json')}")


def replay_command(args) -> None:
    import redis  # only needed here

    r = redis.Redis.from_url(args.redis, decode_responses=True)
    urls = [u.strip() for u in args.detectors.split(",") if u.strip()]
    names = list(SCENARIOS) if args.all else [args.scenario]
    results = {}
    for name in names:
        print(f"\n== {name} ==")
        printed = []

        def show(row):
            if not printed:
                printed.append([max(len(replay_mod._cell(v)), len(v["detectorId"])) for v in row["votes"]])
                print(replay_mod.format_header(row, printed[0]))
            print(replay_mod.format_row(row, printed[0]), flush=True)

        rows = replay_mod.replay_scenario(name, r, urls, args.speed, args.seed, args.browser, on_row=show)
        results[name] = rows
        print(f"-> {replay_mod.write_csv(rows, name, args.results)}")
    print("\n" + replay_mod.format_summary(replay_mod.summarize(results)))
    # A dead detector shows up only as "error" in the table: say why, once per detector
    first = {}
    for rows in results.values():
        for row in rows:
            for v in row["votes"]:
                if v["verdict"] == "error":
                    first.setdefault(v["detectorId"], v["reason"])
    for name, why in first.items():
        print(f"{name}: {why}")


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
    rep.add_argument("--detectors", default="http://localhost:8101,http://localhost:8102,http://localhost:8103")
    rep.add_argument("--speed", type=float, default=0, help="0 = as fast as possible, 1 = real time (10 s gaps)")
    rep.add_argument("--seed", type=int, default=42)
    rep.add_argument("--browser", action=argparse.BooleanOptionalAction, default=True,
                     help="browser-like check-ins (default) or clean ones")
    rep.add_argument("--results", type=Path, default=Path("results"), help="CSV output directory")

    args = parser.parse_args(argv)
    if args.command == "replay":
        replay_command(args)
        return 0
    generate_command(args.out, args.seed, args.scenario or list(SCENARIOS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
