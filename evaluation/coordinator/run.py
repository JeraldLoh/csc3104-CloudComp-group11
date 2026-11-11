"""Coordinator evaluation: runs the experiments against the local stack and saves raw results.

    python evaluation/coordinator/run.py C1                 # every scenario, 5 seeds
    python evaluation/coordinator/run.py C1 --seeds 1       # quick check
    python evaluation/coordinator/run.py C2                 # same journeys, one detector broken at a time
    python evaluation/coordinator/run.py C2 --fresh         # start over instead of resuming
    python evaluation/coordinator/run.py C2 --only "wrong, silenced"   # rerun one fault on every detector,
                                                            # into a copy of the newest C2 result
    python evaluation/coordinator/run.py C4                 # 2 replicas: one killed, the Alert Service down
    python evaluation/coordinator/run.py C5                 # fixed4 vs adaptive vs fixed2 (restarts the
                                                            # Coordinator in each mode, then back in fixed4)

C2, C4 and C5 save each finished condition, so running them again resumes. Starting the stack
for each experiment: README.md. Writes results/<experiment>-<time>.json for analyse.py.
"""
import argparse
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from stack import DETECTORS, Stack, case_of, journey, play
from tools.simulator.scenarios import SCENARIOS

RESULTS = Path(__file__).resolve().parent / "results"


def select_journeys(seeds: int, run_id: str) -> list[dict]:
    """`seeds` journeys per scenario; for off_route, `seeds` that leave the corridor plus every
    near-leg one on the way. Same seeds -> same journeys in every experiment."""
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


def run_journey(stack: Stack, j: dict, wait_s: float = 10, incidents: bool = True, mode: str = "fixed4") -> dict:
    """Play one journey and check every decision's k matches `mode`. C4 passes incidents=False and
    reads them once the Alert Service is surely up again."""
    checkins = j["checkins"]
    rows = play(stack, checkins, timeout_s=wait_s)
    ks = {r["decision"]["k"] for r in rows if r["decision"]}
    if mode == "fixed4" and ks - {4}:
        raise SystemExit(f"decisions asked k={sorted(ks)} detectors: the evaluation needs REDUNDANCY_MODE=fixed4")
    if mode != "fixed4" and ks - {2, 4}:
        raise SystemExit(f"decisions asked k={sorted(ks)} detectors: {mode} asks 2, or 4 after escalating")
    return {"scenario": j["scenario"], "case": j["case"], "farthestM": j["farthestM"], "seed": j["seed"],
            "tripId": checkins[0]["tripId"], "travellerId": checkins[0]["travellerId"], "rows": rows,
            "incidents": stack.incidents(checkins[0]["travellerId"]) if incidents else None}


def c1(stack: Stack, seeds: int, run_id: str) -> dict:
    """C1: the Coordinator's decision vs each detector alone (every decision records all 4 votes)."""
    journeys = []
    for j in select_journeys(seeds, run_id):
        started = time.perf_counter()
        done = run_journey(stack, j)
        undecided = sum(r["decision"] is None for r in done["rows"])
        journeys.append(done)
        print(f"  {j['case']:<16} seed {j['seed']}: {len(done['rows'])} check-ins in "
              f"{time.perf_counter() - started:.1f} s" + (f", {undecided} NOT decided" if undecided else ""), flush=True)
    return {"journeys": journeys}


# C2: each fault, as set on one detector's switch.
FAULTS = {
    "slow": {"mode": "pass", "delayMs": 2000},
    "dead": {"mode": "error"},
    "wrong": {"mode": "wrong"},
    "wrong, silenced": {"mode": "wrong"},          # and reliability 0 in detector:weights
}
HEALTHY = {"mode": "pass", "delayMs": 0}
PARALLEL = 8   # journeys at once: a slow detector costs 500 ms per check-in
WRONG_REASON = "switch wrong"   # how the fault switch marks a vote it inverted


def load_progress(progress: Path, seeds: int, fresh: bool) -> dict | None:
    """The conditions an unfinished run already saved, if it used the same seeds."""
    if fresh or not progress.exists():
        return None
    saved = json.loads(progress.read_text())
    if saved.get("seeds") != seeds:
        raise SystemExit(f"{progress.name} is from a run with --seeds {saved.get('seeds')}: run with those "
                         "seeds to resume it, or with --fresh to start over")
    return saved


def c2(stack: Stack, seeds: int, run_id: str, started_at: str, progress: Path, fresh: bool = False,
       only: str | None = None) -> dict:
    """C2: the C1 journeys with every detector healthy, then with each fault on each detector.
    `only`: just that fault."""
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
    """(timeouts, votes) of the healthy detectors. Many timeouts mean the stack itself was too
    slow (computer asleep), so stop rather than save that."""
    votes = [v for j in journeys for r in j["rows"] if r["decision"]
             for v in r["decision"]["votes"] if v["detectorId"] != broken]
    timeouts = sum(v.get("reason") == "timeout" for v in votes)
    if votes and timeouts / len(votes) > MAX_HEALTHY_TIMEOUTS:
        raise SystemExit(f"{timeouts} of {len(votes)} votes from healthy detectors timed out: the stack was too "
                         "slow during this run (did the computer sleep?). Keep it awake and run again.")
    return timeouts, len(votes)


def check_fault_reached_the_coordinator(journeys: list[dict], fault: str, detector: str) -> None:
    """Stop if the fault doesn't show in the votes (stack started without compose.c2.yaml)."""
    votes = [v for j in journeys for r in j["rows"] if r["decision"]
             for v in r["decision"]["votes"] if v["detectorId"] == detector]
    if fault in ("slow", "dead"):
        ok = all(v["verdict"] == "abstain" for v in votes)
    else:
        ok = any((v.get("reason") or "").startswith(WRONG_REASON) for v in votes)
    if not ok:
        raise SystemExit(f"{detector} doesn't show the '{fault}' fault: start the stack with "
                         "evaluation/coordinator/compose.c2.yaml (see README.md), and keep the "
                         "computer awake (a stalled switch times out instead of answering)")


# C4 (compose.c4.yaml runs 2 Coordinator replicas). The journeys take about 90 s.
REPLICAS = ("coordinator", "coordinator-2")
FIRST_FAULT_S = 5
KILL_EVERY_S = 20        # one replica, alternating
RESTART_AFTER_S = 5      # as Kubernetes would restart the pod
ALERT_DOWN_FOR_S = 45    # a check-in is received 2-3 times, under the 5 before the DLQ
C4_WAIT_S = 120          # per check-in: a redelivery takes the 30 s visibility timeout, maybe twice
SETTLE_S = 60            # after the journeys: last chance for a redelivered check-in


def _event(events: list, action: str, service: str) -> None:
    events.append({"at": datetime.now(timezone.utc).isoformat(), "action": action, "service": service})


def no_fault(stack: Stack, stop, events: list) -> None:
    pass


def kill_replicas(stack: Stack, stop, events: list) -> None:
    """Every KILL_EVERY_S, SIGKILL one replica (alternating) and start it RESTART_AFTER_S later."""
    n, wait = 0, FIRST_FAULT_S
    while not stop.wait(wait):
        target = REPLICAS[n % len(REPLICAS)]
        stack.kill(target)
        _event(events, "kill", target)
        stop.wait(RESTART_AFTER_S)
        stack.start(target)
        _event(events, "start", target)
        n, wait = n + 1, KILL_EVERY_S - RESTART_AFTER_S


def alert_service_down(stack: Stack, stop, events: list) -> None:
    if stop.wait(FIRST_FAULT_S):
        return
    stack.stop("alert-service")
    _event(events, "stop", "alert-service")
    stop.wait(ALERT_DOWN_FOR_S)
    stack.start("alert-service")
    _event(events, "start", "alert-service")


C4_CONDITIONS = {"baseline": no_fault, "replica killed": kill_replicas, "alert service down": alert_service_down}

DECIDED_LINE = re.compile(r"decided (\S+) k=")
SKIPPED_LINE = re.compile(r"skip (\S+): already decided")


def count_lines(text: str, pattern: re.Pattern) -> dict[str, int]:
    counts: dict[str, int] = {}
    for request_id in pattern.findall(text):
        counts[request_id] = counts.get(request_id, 0) + 1
    return counts


def settle(stack: Stack, journeys: list[dict], wait_s: float = SETTLE_S) -> None:
    """Look again for rows not decided within the wait; ones found are marked late."""
    pending = [(j, r) for j in journeys for r in j["rows"] if r["decision"] is None]
    end = time.monotonic() + wait_s
    while pending and time.monotonic() < end:
        still = []
        for j, r in pending:
            if stack.redis.exists(f"decided:{j['tripId']}:{r['seq']}"):
                r["decision"], r["late"] = stack.decision_for(j["travellerId"], r["seq"]), True
            else:
                still.append((j, r))
        pending = still
        time.sleep(1)


def ensure_up(stack: Stack) -> None:
    for service in (*REPLICAS, "alert-service"):
        if not stack.running(service):
            stack.start(service)
    for _ in range(60):   # the Alert Service needs a moment before its incidents can be read
        try:
            stack.check()
            return
        except SystemExit:
            time.sleep(1)
    stack.check()


def c4(stack: Stack, seeds: int, run_id: str, started_at: str, progress: Path, fresh: bool = False) -> dict:
    """C4: the C1 journeys with 2 Coordinator replicas, while nothing / a replica / the Alert
    Service is crashed. Both replicas' logs say how often each check-in was decided or skipped."""
    for service in REPLICAS:
        if not stack.running(service):
            raise SystemExit(f"{service} isn't running: start the stack with evaluation/coordinator/compose.c4.yaml "
                             "(see README.md)")
    saved = load_progress(progress, seeds, fresh)
    if saved:
        run_id, started_at = saved["runId"], saved["startedAt"]
        print(f"  resuming run {run_id}: {len(saved['conditions'])} of {len(C4_CONDITIONS)} conditions already done",
              flush=True)
    out = saved["conditions"] if saved else []
    done = {c["condition"] for c in out}
    for n, (condition, fault) in enumerate(C4_CONDITIONS.items()):
        if condition in done:
            continue
        ensure_up(stack)
        since = datetime.now(timezone.utc).isoformat()
        dlq_before, started = stack.dlq_depth(), time.perf_counter()
        events, stop = [], threading.Event()
        chaos = threading.Thread(target=fault, args=(stack, stop, events), daemon=True)
        chaos.start()
        try:
            with ThreadPoolExecutor(PARALLEL) as pool:
                journeys = list(pool.map(lambda j: run_journey(stack, j, C4_WAIT_S, incidents=False),
                                         select_journeys(seeds, f"{run_id}x{n}")))
        finally:
            stop.set()
            chaos.join()
            ensure_up(stack)
        settle(stack, journeys)
        healthy_timeouts(journeys, None)
        decided = count_lines("".join(stack.logs(s, since) for s in REPLICAS), DECIDED_LINE)
        skipped = count_lines("".join(stack.logs(s, since) for s in REPLICAS), SKIPPED_LINE)
        for j in journeys:
            j["incidents"] = stack.incidents(j["travellerId"])
            for r in j["rows"]:
                request_id = f"{j['tripId']}#{r['seq']}"
                r["decidedCount"], r["skippedCount"] = decided.get(request_id, 0), skipped.get(request_id, 0)
        reports = stack.logs("alert-service", since).count("POST /internal/problems")
        out.append({"condition": condition, "events": events, "dlqAdded": stack.dlq_depth() - dlq_before,
                    "reportsPosted": reports, "journeys": journeys})
        progress.write_text(json.dumps({"runId": run_id, "startedAt": started_at, "seeds": seeds,
                                        "conditions": out}, indent=1))
        rows = [r for j in journeys for r in j["rows"]]
        print(f"  {condition:<19} {len(rows)} check-ins in {time.perf_counter() - started:.0f} s, "
              f"{sum(e['action'] in ('kill', 'stop') for e in events)} crashes, "
              f"{sum(r['decision'] is None for r in rows)} not decided, "
              f"{sum(r['decidedCount'] > 1 for r in rows)} decided twice  [{len(out)}/{len(C4_CONDITIONS)} saved]",
              flush=True)
    return {"runId": run_id, "startedAt": started_at, "parallel": PARALLEL, "conditions": out}


C5_MODES = ("fixed4", "adaptive", "fixed2")


def c5(stack: Stack, seeds: int, run_id: str, started_at: str, progress: Path, fresh: bool = False) -> dict:
    """C5: the C1 journeys once per REDUNDANCY_MODE, ending back in fixed4 whatever happens."""
    saved = load_progress(progress, seeds, fresh)
    if saved:
        run_id, started_at = saved["runId"], saved["startedAt"]
        print(f"  resuming run {run_id}: {len(saved['conditions'])} of {len(C5_MODES)} modes already done", flush=True)
    out = saved["conditions"] if saved else []
    done = {c["mode"] for c in out}
    try:
        for n, mode in enumerate(C5_MODES):
            if mode in done:
                continue
            stack.use_mode(mode)
            started = time.perf_counter()
            journeys = [run_journey(stack, j, mode=mode) for j in select_journeys(seeds, f"{run_id}x{n}")]
            healthy_timeouts(journeys, None)
            ks = [r["decision"]["k"] for j in journeys for r in j["rows"] if r["decision"]]
            if mode != "fixed4" and 2 not in ks:
                raise SystemExit(f"no decision asked 2 detectors: the Coordinator didn't switch to {mode}")
            out.append({"mode": mode, "journeys": journeys})
            progress.write_text(json.dumps({"runId": run_id, "startedAt": started_at, "seeds": seeds,
                                            "conditions": out}, indent=1))
            print(f"  {mode:<9} {len(ks)} check-ins in {time.perf_counter() - started:.0f} s, "
                  f"{sum(ks) / len(ks):.2f} detectors per check-in  [{len(out)}/{len(C5_MODES)} saved]", flush=True)
    finally:
        stack.use_mode("fixed4")
    return {"runId": run_id, "startedAt": started_at, "conditions": out}


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


EXPERIMENTS = {"C1": c1, "C2": c2, "C4": c4, "C5": c5}


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
        if a.experiment in ("C2", "C4", "C5"):
            progress = RESULTS / f"{a.experiment}-progress.json"
            data = EXPERIMENTS[a.experiment](stack, a.seeds, run_id, now.isoformat(), progress, a.fresh)
        else:
            progress = None
            data = EXPERIMENTS[a.experiment](stack, a.seeds, run_id)
        out.write_text(json.dumps({"experiment": a.experiment, "runId": run_id, "startedAt": now.isoformat(),
                                   "seeds": a.seeds, **data}, indent=1))
    if progress:
        progress.unlink(missing_ok=True)
    print(f"saved {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")


if __name__ == "__main__":
    main()
