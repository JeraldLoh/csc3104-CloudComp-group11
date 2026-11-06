"""Coordinator evaluation: runs the experiments against the local stack and saves raw results.

    python evaluation/coordinator/run.py C1                 # every scenario, 5 seeds
    python evaluation/coordinator/run.py C1 --seeds 1       # quick check

Needs the local stack (services/analysis-dev) with the real detectors, in fixed4 mode:

    docker compose -f services/analysis-dev/compose.yaml up --build -d

Writes evaluation/coordinator/results/<experiment>-<time>.json; analyse.py takes it from there.
Environment: REDIS_URL, AWS_ENDPOINT_URL, COORDINATOR_URL, ALERT_SERVICE_URL (defaults: the
stack's ports on localhost).
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from stack import Stack, journey, off_contract, play
from tools.simulator.scenarios import SCENARIOS

RESULTS = Path(__file__).resolve().parent / "results"


def c1(stack: Stack, seeds: int, run_id: str) -> dict:
    """C1: the Coordinator's decision vs each detector alone. In fixed4 every decision records
    all 4 detectors' votes, so one pass gives both. Journeys whose labels don't match the
    contract's definitions are skipped (and recorded), and the next seed is tried."""
    journeys, skipped = [], []
    for scenario in SCENARIOS:
        chosen, seed = 0, 0
        while chosen < seeds:
            seed += 1
            if seed > seeds * 20:
                raise SystemExit(f"{scenario}: only {chosen} usable journeys in {seed - 1} seeds")
            checkins = journey(scenario, seed, run_id)
            reason = off_contract(scenario, checkins)
            if reason:
                skipped.append({"scenario": scenario, "seed": seed, "reason": reason})
                continue
            chosen += 1
            started = time.perf_counter()
            rows = play(stack, checkins)
            undecided = sum(r["decision"] is None for r in rows)
            ks = {r["decision"]["k"] for r in rows if r["decision"]}
            if ks - {4}:
                raise SystemExit(f"decisions asked k={sorted(ks)} detectors: C1 needs REDUNDANCY_MODE=fixed4")
            journeys.append({"scenario": scenario, "seed": seed, "tripId": checkins[0]["tripId"],
                             "travellerId": checkins[0]["travellerId"], "rows": rows,
                             "incidents": stack.incidents(checkins[0]["travellerId"])})
            print(f"  {scenario:<16} seed {seed}: {len(rows)} check-ins in {time.perf_counter() - started:.1f} s"
                  + (f", {undecided} NOT decided" if undecided else ""), flush=True)
    if skipped:
        print(f"  skipped {len(skipped)} journeys whose labels don't match the contract (see 'skipped')")
    return {"journeys": journeys, "skipped": skipped}


EXPERIMENTS = {"C1": c1}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("experiment", choices=sorted(EXPERIMENTS))
    p.add_argument("--seeds", type=int, default=5, help="journeys per scenario (different random seeds)")
    a = p.parse_args()

    stack = Stack()
    stack.check()
    now = datetime.now(timezone.utc)
    run_id = f"{a.experiment}{now.strftime('%m%d%H%M%S')}"
    print(f"{a.experiment}: run {run_id}", flush=True)
    data = EXPERIMENTS[a.experiment](stack, a.seeds, run_id)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{a.experiment}-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps({"experiment": a.experiment, "runId": run_id, "startedAt": now.isoformat(),
                               "seeds": a.seeds, **data}, indent=1))
    print(f"saved {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")


if __name__ == "__main__":
    main()
