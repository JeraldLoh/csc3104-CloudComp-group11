"""Coordinator evaluation: runs the experiments against the local stack and saves raw results.

    python evaluation/coordinator/run.py C1                 # every scenario, 5 seeds
    python evaluation/coordinator/run.py C1 --seeds 1       # quick check
    python evaluation/coordinator/run.py C2                 # same journeys, one detector broken at a time
    python evaluation/coordinator/run.py C2 --fresh         # start over instead of resuming
    python evaluation/coordinator/run.py C2 --only "wrong, silenced"   # rerun one fault on every detector,
                                                            # into a copy of the newest C2 result

C2 takes about 45 minutes, so each finished condition is saved to results/C2-progress.json at
once. If the run stops (laptop asleep, stack too slow), running it again with the same --seeds
resumes from the next condition; the progress file is removed when all of them are done.

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
WRONG_REASON = "switch wrong"   # how the fault switch marks a vote it inverted (services/fault-switch)


def load_progress(progress: Path, seeds: int, fresh: bool) -> dict | None:
    """The conditions an earlier, unfinished C2 run already saved, if it used the same seeds."""
    if fresh or not progress.exists():
        return None
    saved = json.loads(progress.read_text())
    if saved.get("seeds") != seeds:
        raise SystemExit(f"{progress.name} is from a run with --seeds {saved.get('seeds')}: run with those "
                         "seeds to resume it, or with --fresh to start over")
    return saved


def c2(stack: Stack, seeds: int, run_id: str, started_at: str, progress: Path, fresh: bool = False,
       only: str | None = None) -> dict:
    """C2: the same journeys as C1 with every detector healthy, then with one detector broken at
    a time: slow, dead, wrong, and wrong with its reliability set to 0. Each finished condition
    is saved to `progress`, and a rerun resumes after the last one saved. `only`: just that fault."""
    conditions = [("healthy", None)] + [(f, d) for d in DETECTORS for f in FAULTS]
    if only:
        conditions = [(f, d) for f, d in conditions if f == only]
    saved = load_progress(progress, seeds, fresh)
    if saved:
        run_id, started_at = saved["runId"], saved["startedAt"]
        print(f"  resuming run {run_id}: {len(saved['conditions'])} of {len(conditions)} conditions already done",
              flush=True)
    out = saved["conditions"] if saved else []
    done = {(c["fault"], c["detector"]) for c in out}
    for n, (fault, detector) in enumerate(conditions):
        if (fault, detector) in done:
            continue
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
        progress.write_text(json.dumps({"runId": run_id, "startedAt": started_at, "seeds": seeds,
                                        "conditions": out}, indent=1))
        print(f"  {fault:<16} {detector or '':<15} {sum(len(j['rows']) for j in journeys)} check-ins in "
              f"{time.perf_counter() - started:.0f} s, {timeouts[0]} timeouts on healthy detectors"
              + (f", {undecided} NOT decided" if undecided else "") + f"  [{len(out)}/{len(conditions)} saved]",
              flush=True)
    for d in DETECTORS:
        stack.switch(d, **HEALTHY)
    stack.clear_weights()
    return {"runId": run_id, "startedAt": started_at, "parallel": PARALLEL, "conditions": out}


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
        ok = any((v.get("reason") or "").startswith(WRONG_REASON) for v in votes)
    if not ok:
        raise SystemExit(f"{detector} doesn't show the '{fault}' fault: start the stack with "
                         "evaluation/coordinator/compose.c2.yaml (see the top of this file), and keep the "
                         "computer awake (a stalled switch times out instead of answering)")


def newest_c2() -> Path:
    runs = sorted(RESULTS.glob("C2-*T*Z.json"))
    if not runs:
        raise SystemExit("--only replaces conditions in an earlier C2 result, and there is none: run C2 first")
    return runs[-1]


def merge_rerun(base: dict, rerun: dict, only: str, run_id: str, at: str) -> dict:
    """`base` with its `only` conditions replaced by the rerun's, in the same order."""
    new = {(c["fault"], c["detector"]): c for c in rerun["conditions"]}
    conditions = [new.get((c["fault"], c["detector"]), c) for c in base["conditions"]]
    reruns = base.get("reruns", []) + [{"fault": only, "runId": run_id, "at": at}]
    return {**base, "conditions": conditions, "reruns": reruns}


EXPERIMENTS = {"C1": c1, "C2": c2}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("experiment", choices=sorted(EXPERIMENTS))
    p.add_argument("--seeds", type=int, default=5, help="journeys per scenario (different random seeds)")
    p.add_argument("--fresh", action="store_true", help="C2: start over instead of resuming an unfinished run")
    p.add_argument("--only", choices=sorted(FAULTS), help="C2: rerun just this fault (on every detector) and "
                   "replace it in a copy of the newest C2 result")
    a = p.parse_args()
    if a.only and a.experiment != "C2":
        p.error("--only is for C2")

    stack = Stack()
    stack.check()
    now = datetime.now(timezone.utc)
    run_id = f"{a.experiment}{now.strftime('%m%d%H%M%S')}"
    print(f"{a.experiment}: run {run_id}", flush=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{a.experiment}-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    if a.only:
        base_file = newest_c2()
        base = json.loads(base_file.read_text())
        if base["seeds"] != a.seeds:
            raise SystemExit(f"{base_file.name} used --seeds {base['seeds']}: rerun with the same seeds")
        progress = RESULTS / "C2-only-progress.json"
        rerun = c2(stack, a.seeds, run_id, now.isoformat(), progress, a.fresh, only=a.only)
        out.write_text(json.dumps(merge_rerun(base, rerun, a.only, rerun["runId"], now.isoformat()), indent=1))
        print(f"  replaced the '{a.only}' conditions of {base_file.name}")
    else:
        if a.experiment == "C2":
            progress = RESULTS / "C2-progress.json"
            data = c2(stack, a.seeds, run_id, now.isoformat(), progress, a.fresh)
        else:
            progress = None
            data = EXPERIMENTS[a.experiment](stack, a.seeds, run_id)
        out.write_text(json.dumps({"experiment": a.experiment, "runId": run_id, "startedAt": now.isoformat(),
                                   "seeds": a.seeds, **data}, indent=1))
    if progress:
        progress.unlink(missing_ok=True)   # finished: nothing left to resume
    print(f"saved {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")


if __name__ == "__main__":
    main()
