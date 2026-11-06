"""Turns the newest results/<experiment>-*.json into numbers: results/summary.json and RESULTS.md.

    python evaluation/coordinator/analyse.py

Scores each "judge" against the Coordinator's decision and each detector's own vote taken 
as if it were the decision.
  - Detected: a journey with a problem counts as caught if the judge says that problem, of the
    right type, on at least one check-in labelled with it. Delay: seconds from the first
    labelled check-in to the first one caught.
  - False alarm: "problem" on a check-in whose label is normal (poor_gps check-ins, labelled
    "abstain", must not alert either), or a problem of the wrong type.
  - No answer: abstain (a detector) or inconclusive (the Coordinator).
  - Alerts: incidents the Alert Service actually created; on journeys with no problem, every
    one is an alert a caregiver would get for nothing.
A detector is only scored on the problem types it checks (contracts/problem-definitions.md).
"""
import json
import statistics
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

PROBLEMS = ("off_route", "wrong_direction", "long_dwell")
JUDGES = ("combined", "route-corridor", "heading-speed", "dwell-time", "route-progress")
COVERS = {
    "combined": set(PROBLEMS),
    "route-corridor": {"off_route"},
    "heading-speed": {"wrong_direction"},
    "dwell-time": {"long_dwell"},
    "route-progress": {"off_route", "wrong_direction"},
}
NOT_PROBLEMS = {"normal", "abstain"}


def answer(row: dict, judge: str) -> tuple[str, str | None]:
    """(verdict, problemType) of one judge on one check-in. verdict: problem | normal | none."""
    d = row["decision"]
    if d is None:
        return "none", None
    if judge == "combined":
        verdict, ptype = d["decision"], d.get("problemType")
    else:
        vote = next((v for v in d["votes"] if v["detectorId"] == judge), None)
        if vote is None:
            return "none", None
        verdict, ptype = vote["verdict"], vote.get("problemType")
    return (verdict if verdict in ("problem", "normal") else "none"), ptype


def _seconds(a: str, b: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds()


def score_journey(journey: dict, judge: str) -> dict:
    rows = journey["rows"]
    out = {"checkins": len(rows), "notProblem": 0, "falseAlarms": 0, "noAnswer": 0, "undecided": 0}
    problem = next((r["expected"] for r in rows if r["expected"] in PROBLEMS), None)
    first_label = first_hit = first_any = None
    for r in rows:
        verdict, ptype = answer(r, judge)
        out["undecided"] += r["decision"] is None
        out["noAnswer"] += verdict == "none"
        if r["expected"] in NOT_PROBLEMS:
            out["notProblem"] += 1
        if verdict == "problem" and ptype != r["expected"]:
            out["falseAlarms"] += 1
        if r["expected"] == problem:
            first_label = first_label or r["timestamp"]
            if first_any is None and verdict == "problem":
                first_any = r["timestamp"]
            if first_hit is None and verdict == "problem" and ptype == problem:
                first_hit = r["timestamp"]
    if problem and problem in COVERS[judge]:
        out["problem"] = problem
        out["detected"] = first_hit is not None
        out["delayS"] = _seconds(first_label, first_hit) if first_hit else None
        # Any alert at all, even of another type: the caregiver is told something is wrong.
        out["anyDelayS"] = _seconds(first_label, first_any) if first_any else None
    return out


def c1(data: dict) -> dict:
    journeys = data["journeys"]
    judges = {}
    for judge in JUDGES:
        scores = [score_journey(j, judge) for j in journeys]
        per_problem = {}
        for p in PROBLEMS:
            mine = [s for s in scores if s.get("problem") == p]
            if not mine:
                continue
            delays = [s["delayS"] for s in mine if s["delayS"] is not None]
            any_delays = [s["anyDelayS"] for s in mine if s["anyDelayS"] is not None]
            per_problem[p] = {"journeys": len(mine), "detected": sum(s["detected"] for s in mine),
                              "medianDelayS": statistics.median(delays) if delays else None,
                              "medianAnyDelayS": statistics.median(any_delays) if any_delays else None}
        checkins = sum(s["checkins"] for s in scores)
        judges[judge] = {
            "checkins": checkins,
            "falseAlarms": sum(s["falseAlarms"] for s in scores),
            "falseAlarmRate": round(sum(s["falseAlarms"] for s in scores) / checkins, 4),
            "noAnswerRate": round(sum(s["noAnswer"] for s in scores) / checkins, 4),
            "problems": per_problem,
        }
    alerts = {"falseAlerts": 0, "journeysWithoutProblem": 0, "missedAlerts": 0, "journeysWithProblem": 0,
              "wrongTypeAlerts": 0}
    for j in journeys:
        problem = next((r["expected"] for r in j["rows"] if r["expected"] in PROBLEMS), None)
        types = [i["problemType"] for i in j["incidents"]]
        if problem is None:
            alerts["journeysWithoutProblem"] += 1
            alerts["falseAlerts"] += len(types)
        else:
            alerts["journeysWithProblem"] += 1
            alerts["missedAlerts"] += problem not in types
            alerts["wrongTypeAlerts"] += sum(t != problem for t in types)
    latencies = sorted(r["endToEndMs"] for j in journeys for r in j["rows"] if r["endToEndMs"] is not None)
    return {"runId": data["runId"], "seeds": data["seeds"], "journeys": len(journeys),
            "skipped": data.get("skipped", []),
            "undecided": sum(r["decision"] is None for j in journeys for r in j["rows"]),
            "judges": judges, "alerts": alerts,
            "endToEndMs": {"p50": _pct(latencies, 50), "p95": _pct(latencies, 95)} if latencies else None}


def _pct(xs: list[float], p: int) -> float:
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def c1_markdown(s: dict) -> str:
    lines = [f"## C1: the Coordinator's decision vs each detector alone",
             "",
             f"Run `{s['runId']}`: {s['journeys']} journeys ({s['seeds']} per scenario), "
             f"{s['judges']['combined']['checkins']} check-ins, {s['undecided']} not decided. "
             "Detected = journeys caught / journeys with that problem; delay = median seconds from the "
             "first labelled check-in to the first caught with the right type (check-ins are 10 s "
             "apart); \"any alert\" when an alert of another type came sooner. "
             "– = not a problem that detector checks.",
             "",
             "| Judge | " + " | ".join(f"{p} detected | delay" for p in PROBLEMS) + " | false alarms | no answer |",
             "|---|" + "---|---|" * len(PROBLEMS) + "---|---|"]
    for judge, j in s["judges"].items():
        cells = []
        for p in PROBLEMS:
            if p not in j["problems"]:
                cells += ["–", "–"]
                continue
            pp = j["problems"][p]
            delay = "–" if pp["medianDelayS"] is None else f"{pp['medianDelayS']:.0f} s"
            if pp["medianAnyDelayS"] is not None and pp["medianAnyDelayS"] != pp["medianDelayS"]:
                delay += f" (any alert {pp['medianAnyDelayS']:.0f} s)"
            cells += [f"{pp['detected']}/{pp['journeys']}", delay]
        name = "**Coordinator (combined)**" if judge == "combined" else judge
        lines.append(f"| {name} | " + " | ".join(cells) +
                     f" | {j['falseAlarms']} ({j['falseAlarmRate']:.1%}) | {j['noAnswerRate']:.1%} |")
    a = s["alerts"]
    lines += ["",
              f"Alerts actually created by the Alert Service: {a['journeysWithProblem'] - a['missedAlerts']}"
              f"/{a['journeysWithProblem']} journeys with a problem got an alert of the right type "
              f"({a['wrongTypeAlerts']} alerts of another type); {a['falseAlerts']} alerts on the "
              f"{a['journeysWithoutProblem']} journeys without a problem."]
    if s["skipped"]:
        by = {}
        for k in s["skipped"]:
            by.setdefault(k["scenario"], []).append(k["seed"])
        lines.append("Skipped (labels don't match `contracts/problem-definitions.md`; the next seed was used): " +
                     "; ".join(f"{sc} seeds {', '.join(map(str, seeds))}" for sc, seeds in by.items()) +
                     f". Example: {s['skipped'][0]['reason']}.")
    if s["endToEndMs"]:
        lines.append(f"Check-in on the queue to decision recorded: p50 {s['endToEndMs']['p50']:.0f} ms, "
                     f"p95 {s['endToEndMs']['p95']:.0f} ms (one machine, light load; C3 measures load).")
    return "\n".join(lines)


ANALYSES = {"C1": (c1, c1_markdown)}


def newest(experiment: str) -> Path | None:
    runs = sorted(RESULTS.glob(f"{experiment}-*.json"))
    return runs[-1] if runs else None


def main() -> None:
    summary, sections = {}, []
    for name, (analyse, markdown) in ANALYSES.items():
        path = newest(name)
        if path is None:
            continue
        summary[name] = analyse(json.loads(path.read_text()))
        sections.append(markdown(summary[name]))
    if not summary:
        raise SystemExit("no results yet: run run.py first")
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=1))
    (HERE / "RESULTS.md").write_text("# Coordinator evaluation: results\n\nGenerated by `analyse.py` from "
                                     "the newest run of each experiment in `results/`; see "
                                     "[README.md](README.md) for how they work.\n\n" + "\n\n".join(sections) + "\n")
    print("\n\n".join(sections))
    print("\nwrote results/summary.json and RESULTS.md")


if __name__ == "__main__":
    main()
