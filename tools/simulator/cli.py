"""Command line: ``python -m tools.simulator generate --out services/detectors/tests/fixtures/``."""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional

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
        checkins = generate(name, seed)
        (out / f"{name}.json").write_text(_dump_checkins(checkins))
        print(f"{name}: {len(checkins)} check-ins -> {out / (name + '.json')}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.simulator")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="write one fixture file per scenario, plus route.json")
    gen.add_argument("--out", type=Path, default=Path(DEFAULT_OUT), help="output directory")
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--scenario", action="append", choices=SCENARIOS,
                     help="only this scenario (repeatable); default is all")

    args = parser.parse_args(argv)
    generate_command(args.out, args.seed, args.scenario or list(SCENARIOS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
