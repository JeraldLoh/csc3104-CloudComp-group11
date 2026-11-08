"""Coordinator evaluation: runs the experiments against the local stack and saves raw results.

    python evaluation/coordinator/run.py C1                 # every scenario, 5 seeds
    python evaluation/coordinator/run.py C1 --seeds 1       # quick check
    python evaluation/coordinator/run.py C2                 # same journeys, one detector broken at a time

Needs the local stack (services/analysis-dev) with the real detectors, in fixed4 mode:

    docker compose -f services/analysis-dev/compose.yaml up --build -d                  # C1
    docker compose -f services/analysis-dev/compose.yaml \\
                   -f evaluation/coordinator/compose.c2.yaml up --build -d               # C2 (fault switches)

Writes evaluation/coordinator/results/<experiment>-<time>.json; analyse.py takes it from there.
Environment: REDIS_URL, AWS_ENDPOINT_URL, COORDINATOR_URL, ALERT_SERVICE_URL (defaults: the
stack's ports on localhost).
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from stack import DETECTORS, Stack, case_of, journey, play
from tools.simulator.scenarios import SCENARIOS

RESULTS = Path(__file__).resolve().parent / "results"


def select_journeys(seeds: int, run_id: str) -> list[dict]:
    """`seeds` journeys per scenario. off_route journeys come in two kinds (stack.case_of): seeds
    are drawn until there are `seeds` that leave the corridor, and every one that wanders near
    another leg on the way is kept too, as its own case. Same seeds -> same journeys, so C1 and
    C2 replay identical check-ins."""
    out = []
    for scenario in SCENARIOS:
        kept, seed = 0, 0
        while kept < seeds:
            seed += 1
            if seed > seeds * 20:
                raise SystemExit(f"{scenario}: only {kept} journeys of that kind in {seed - 1} seeds")
            checkins = journey(scenario, seed, run_id)
            case, farthest = case_of(scenario, checkins)
            kept += case == scenario
            out.append({"scenario": scenario, "case": case, "farthestM": farthest, "seed": seed, "checkins": checkins})
    return out


def run_journey(stack: Stack, j: dict) -> dict:
    checkins = j["checkins"]
    rows = play(stack, checkins)
    ks = {r["decision"]["k"] for r in rows if r["decision"]}
    if ks - {4}:
        raise SystemExit(f"decisions asked k={sorted(ks)} detectors: the evaluation needs REDUNDANCY_MODE=fixed4")
    return {"scenario": j["scenario"], "case": j["case"], "farthestM": j["farthestM"], "seed": j["seed"],
            "tripId": checkins[0]["tripId"], "travellerId": checkins[0]["travellerId"], "rows": rows,
            "incidents": stack.incidents(checkins[0]["travellerId"])}


def c1(stack: Stack, seeds: int, run_id: str) -> dict:
    """C1: the Coordinator's decision vs each detector alone. In fixed4 every decision records
    all 4 detectors' votes, so one pass gives both. One journey at a time."""
    journeys = []
    for j in select_journeys(seeds, run_id):
        started = time.perf_counter()
        done = run_journey(stack, j)
        undecided = sum(r["decision"] is None for r in done["rows"])
        journeys.append(done)
        print(f"  {j['case']:<16} seed {j['seed']}: {len(done['rows'])} check-ins in "
              f"{time.perf_counter() - started:.1f} s" + (f", {undecided} NOT decided" if undecided else ""), flush=True)
    return {"journeys": journeys}


# C2: what each fault does to one detector (POST /control on its switch, compose.c2.yaml).
FAULTS = {
    "slow": {"mode": "pass", "delayMs": 2000},   # over the Coordinator's 500 ms timeout
    "dead": {"mode": "error"},                     # answers 500, as a crashed detector would fail
    "wrong": {"mode": "wrong"},                    # the real vote inverted
    "wrong, silenced": {"mode": "wrong"},          # ... and its reliability set to 0 in detector:weights
}
HEALTHY = {"mode": "pass", "delayMs": 0}
PARALLEL = 8   # journeys at once: a slow detector costs 500 ms per check-in


def c2(stack: Stack, seeds: int, run_id: str) -> dict:
    """C2: the same journeys as C1 with every detector healthy, then with one detector broken at
    a time: slow, dead, wrong, and wrong with its reliability set to 0."""
    conditions = [("healthy", None)] + [(f, d) for d in DETECTORS for f in FAULTS]
    out = []
    for n, (fault, detector) in enumerate(conditions):
        for d in DETECTORS:
            stack.switch(d, **HEALTHY)
        stack.clear_weights()
        if detector:
            stack.switch(detector, **FAULTS[fault])
            if fault == "wrong, silenced":
                stack.set_weight(detector, 0)
        started = time.perf_counter()
        with ThreadPoolExecutor(PARALLEL) as pool:
            journeys = list(pool.map(lambda j: run_journey(stack, j), select_journeys(seeds, f"{run_id}x{n}")))
        timeouts = healthy_timeouts(journeys, detector)
        if detector:
            check_fault_reached_the_coordinator(journeys, fault, detector)
        undecided = sum(r["decision"] is None for j in journeys for r in j["rows"])
        out.append({"fault": fault, "detector": detector, "healthyTimeouts": timeouts, "journeys": journeys})
        print(f"  {fault:<16} {detector or '':<15} {sum(len(j['rows']) for j in journeys)} check-ins in "
              f"{time.perf_counter() - started:.0f} s, {timeouts[0]} timeouts on healthy detectors"
              + (f", {undecided} NOT decided" if undecided else ""), flush=True)
    for d in DETECTORS:
        stack.switch(d, **HEALTHY)
    stack.clear_weights()
    return {"parallel": PARALLEL, "conditions": out}


MAX_HEALTHY_TIMEOUTS = 0.01   # of the healthy detectors' votes


def healthy_timeouts(journeys: list[dict], broken: str | None) -> tuple[int, int]:
    """(timeouts, votes) of the detectors that should be healthy. They answer in a few ms, so any
    real number of timeouts means the stack itself was too slow (laptop asleep, overloaded): the
    run would then measure that, not the fault. Stop rather than save it."""
    votes = [v for j in journeys for r in j["rows"] if r["decision"]
             for v in r["decision"]["votes"] if v["detectorId"] != broken]
    timeouts = sum(v.get("reason") == "timeout" for v in votes)
    if votes and timeouts / len(votes) > MAX_HEALTHY_TIMEOUTS:
        raise SystemExit(f"{timeouts} of {len(votes)} votes from healthy detectors timed out: the stack was too "
                         "slow during this run (did the computer sleep?). Keep it awake and run again.")
    return timeouts, len(votes)


def check_fault_reached_the_coordinator(journeys: list[dict], fault: str, detector: str) -> None:
    """Stop if the Coordinator isn't calling the switches (stack started without compose.c2.yaml):
    the run would silently measure healthy detectors."""
    votes = [v for j in journeys for r in j["rows"] if r["decision"]
             for v in r["decision"]["votes"] if v["detectorId"] == detector]
    if fault in ("slow", "dead"):
        ok = all(v["verdict"] == "abstain" for v in votes)
    else:
        ok = any((v.get("reason") or "").startswith("fake wrong") for v in votes)
    if not ok:
        raise SystemExit(f"{detector} doesn't show the '{fault}' fault: start the stack with "
                         "evaluation/coordinator/compose.c2.yaml (see the top of this file), and keep the "
                         "computer awake (a stalled switch times out instead of answering)")


EXPERIMENTS = {"C1": c1, "C2": c2}


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
