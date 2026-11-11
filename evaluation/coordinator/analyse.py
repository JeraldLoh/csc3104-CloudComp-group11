"""Turns the newest results/<experiment>-*.json into numbers: results/summary.json and RESULTS.md.

    python evaluation/coordinator/analyse.py

Each "judge" (the Coordinator's decision, and each detector's vote on its own) is scored:
  - Detected: the right problem type raised on at least one check-in labelled with it.
    Delay: seconds from the first labelled check-in to the first caught.
  - False alarm: "problem" on a check-in labelled normal or poor GPS.
  - Wrong type: "problem" during a real problem, but none of the types raised is the right one.
  - No answer: abstain (a detector) or inconclusive (the Coordinator).
  - Alerts: incidents the Alert Service created; on journeys without a problem, each is a false alert.
The Coordinator is judged on every type it raised; a detector only on the types it checks.
"""
import json
import statistics
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

PROBLEMS = ("off_route", "wrong_direction", "long_dwell")
CASES = ("off_route", "off_route_near", "wrong_direction", "long_dwell")
CASE_NAMES = {"off_route": "off route (leaves the corridor)", "off_route_near": "off route near another leg",
              "wrong_direction": "wrong direction", "long_dwell": "long dwell"}
JUDGES = ("combined", "route-corridor", "heading-speed", "dwell-time", "route-progress")
COVERS = {
    "combined": set(PROBLEMS),
    "route-corridor": {"off_route"},
    "heading-speed": {"wrong_direction"},
    "dwell-time": {"long_dwell"},
    "route-progress": {"off_route", "wrong_direction"},
}
NOT_PROBLEMS = {"normal", "abstain"}
TRACKING_LOST = "tracking_lost"


def answer(row: dict, judge: str) -> tuple[str, set]:
    """(verdict, problem types raised) of one judge on one check-in. verdict: problem | normal | none."""
    d = row["decision"]
    if d is None:
        return "none", set()
    if judge == "combined":
        verdict = d["decision"]
        types = {a["problemType"] for a in d.get("alerts") or []} or {d.get("problemType")}
    else:
        vote = next((v for v in d["votes"] if v["detectorId"] == judge), None)
        if vote is None:
            return "none", set()
        verdict, types = vote["verdict"], {vote.get("problemType")}
    if verdict not in ("problem", "normal"):
        return "none", set()
    return verdict, (types if verdict == "problem" else set())


def _seconds(a: str, b: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds()


def score_journey(journey: dict, judge: str) -> dict:
    rows = journey["rows"]
    out = {"checkins": len(rows), "notProblem": 0, "falseAlarms": 0, "wrongType": 0, "noAnswer": 0,
           "undecided": 0}
    problem = next((r["expected"] for r in rows if r["expected"] in PROBLEMS), None)
    first_label = first_hit = first_any = None
    for r in rows:
        verdict, types = answer(r, judge)
        out["undecided"] += r["decision"] is None
        out["noAnswer"] += verdict == "none"
        if r["expected"] in NOT_PROBLEMS:
            out["notProblem"] += 1
            out["falseAlarms"] += verdict == "problem"
        elif verdict == "problem" and r["expected"] not in types:
            out["wrongType"] += 1
        if r["expected"] == problem:
            first_label = first_label or r["timestamp"]
            if first_any is None and verdict == "problem":
                first_any = r["timestamp"]
            if first_hit is None and problem in types:
                first_hit = r["timestamp"]
    if problem and problem in COVERS[judge]:
        out["problem"] = journey.get("case") or problem
        out["detected"] = first_hit is not None
        out["delayS"] = _seconds(first_label, first_hit) if first_hit else None
        out["anyDelayS"] = _seconds(first_label, first_any) if first_any else None
    return out


def c1(data: dict) -> dict:
    journeys = data["journeys"]
    judges = {}
    for judge in JUDGES:
        scores = [score_journey(j, judge) for j in journeys]
        per_problem = {}
        for p in CASES:
            mine = [s for s in scores if s.get("problem") == p]
            if not mine:
                continue
            delays = [s["delayS"] for s in mine if s["delayS"] is not None]
            any_delays = [s["anyDelayS"] for s in mine if s["anyDelayS"] is not None]
            per_problem[p] = {"journeys": len(mine), "detected": sum(s["detected"] for s in mine),
                              "medianDelayS": statistics.median(delays) if delays else None,
                              "medianAnyDelayS": statistics.median(any_delays) if any_delays else None}
        checkins = sum(s["checkins"] for s in scores)
        not_problem = sum(s["notProblem"] for s in scores)
        judges[judge] = {
            "checkins": checkins,
            "falseAlarms": sum(s["falseAlarms"] for s in scores),
            "falseAlarmRate": round(sum(s["falseAlarms"] for s in scores) / not_problem, 4) if not_problem else 0.0,
            "wrongType": sum(s["wrongType"] for s in scores),
            "noAnswerRate": round(sum(s["noAnswer"] for s in scores) / checkins, 4),
            "problems": per_problem,
        }
    # What caregivers got. tracking_lost is counted on its own: on poor_gps it's the right answer.
    alerts = {"withoutProblem": {"journeys": 0, "falseAlerts": 0}, "cases": {}, "trackingLost": {}}
    for j in journeys:
        problem = next((r["expected"] for r in j["rows"] if r["expected"] in PROBLEMS), None)
        types = {i["problemType"] for i in j["incidents"]}
        lost = alerts["trackingLost"].setdefault(j.get("scenario") or "?", [0, 0])
        lost[0] += TRACKING_LOST in types
        lost[1] += 1
        if problem is None:
            alerts["withoutProblem"]["journeys"] += 1
            alerts["withoutProblem"]["falseAlerts"] += sum(i["problemType"] != TRACKING_LOST for i in j["incidents"])
        else:
            c = alerts["cases"].setdefault(j.get("case") or problem, {"journeys": 0, "rightType": 0, "anyAlert": 0})
            c["journeys"] += 1
            c["rightType"] += problem in types
            c["anyAlert"] += bool(types)
    latencies = sorted(r["endToEndMs"] for j in journeys for r in j["rows"] if r["endToEndMs"] is not None)
    return {"runId": data["runId"], "seeds": data["seeds"], "journeys": len(journeys),
            "undecided": sum(r["decision"] is None for j in journeys for r in j["rows"]),
            "judges": judges, "alerts": alerts,
            "endToEndMs": {"p50": _pct(latencies, 50), "p95": _pct(latencies, 95)} if latencies else None}


def _pct(xs: list[float], p: int) -> float:
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def c1_markdown(s: dict) -> str:
    lines = [f"## C1: the Coordinator's decision vs each detector alone",
             "",
             f"Run `{s['runId']}`: {s['journeys']} journeys ({s['seeds']} per scenario, plus every off-route "
             f"journey that stayed near another leg on the way), {s['judges']['combined']['checkins']} "
             f"check-ins, {s['undecided']} not decided. "
             "Detected = journeys caught / journeys with that problem; delay = median seconds from the "
             "first labelled check-in to the first caught with the right type (check-ins are 10 s "
             "apart); \"any alert\" when an alert of another type came sooner. "
             "– = not a problem that detector checks. *Off route near another leg*: labelled off route "
             "by the simulator, but never more than 100 m from some part of the route (it wanders "
             "towards the road to the bus stop), so outside what route-corridor can see.",
             "",
             "False alarms: check-ins with nothing wrong where it said problem (rate over those check-ins). "
             "Wrong type: check-ins during a real problem where it raised only other problem types.",
             "",
             "| Judge | " + " | ".join(f"{CASE_NAMES[p]} | delay" for p in CASES) +
             " | false alarms | wrong type | no answer |",
             "|---|" + "---|---|" * len(CASES) + "---|---|---|"]
    for judge, j in s["judges"].items():
        cells = []
        for p in CASES:
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
                     f" | {j['falseAlarms']} ({j['falseAlarmRate']:.1%}) | {j['wrongType']} | {j['noAnswerRate']:.1%} |")
    a = s["alerts"]
    lines += ["",
              "What caregivers got (incidents the Alert Service created):",
              "",
              "| Case | journeys | an alert of the right type | any alert |",
              "|---|---|---|---|"]
    for case in CASES:
        if case in a["cases"]:
            c = a["cases"][case]
            lines.append(f"| {CASE_NAMES[case]} | {c['journeys']} | {c['rightType']} | {c['anyAlert']} |")
    w = a["withoutProblem"]
    lines += ["", f"Journeys without a problem: {w['falseAlerts']} alerts on {w['journeys']} journeys "
                  "(not counting *tracking lost*)."]
    if a.get("trackingLost"):
        lines.append("*Tracking lost* (some problem types couldn't be checked for 2 min) raised on: " +
                     ", ".join(f"{sc} {n}/{total}" for sc, (n, total) in a["trackingLost"].items()) + " journeys.")
    if s["endToEndMs"]:
        lines.append(f"Check-in on the queue to decision recorded: p50 {s['endToEndMs']['p50']:.0f} ms, "
                     f"p95 {s['endToEndMs']['p95']:.0f} ms (one machine, light load; C3 measures load).")
    return "\n".join(lines)


OUTAGE_REASONS = ("detectors failed", "detectors silenced")


def outage_noticed(journey: dict) -> bool:
    """tracking_lost was sent because detectors failed or were silenced."""
    return any((r["decision"] or {}).get("uncheckedReason") in OUTAGE_REASONS
               and any(a.get("problemType") == TRACKING_LOST for a in (r["decision"] or {}).get("alerts") or [])
               for r in journey["rows"])


def c2(data: dict) -> dict:
    """Each condition scored exactly like C1; one row per condition."""
    rows = []
    for cond in data["conditions"]:
        s = c1({"runId": data["runId"], "seeds": data["seeds"], "journeys": cond["journeys"]})
        combined = s["judges"]["combined"]["problems"]
        cases = s["alerts"]["cases"]
        rows.append({
            "fault": cond["fault"], "detector": cond["detector"],
            "caught": {c: [combined[c]["detected"], combined[c]["journeys"]] for c in CASES if c in combined},
            "delayS": {c: combined[c]["medianDelayS"] for c in CASES if c in combined},
            "alerted": [sum(c["anyAlert"] for c in cases.values()), sum(c["journeys"] for c in cases.values())],
            "falseAlerts": s["alerts"]["withoutProblem"]["falseAlerts"],
            "journeysWithoutProblem": s["alerts"]["withoutProblem"]["journeys"],
            "outageNoticed": [sum(outage_noticed(j) for j in cond["journeys"]), len(cond["journeys"])],
            "falseAlarmRate": s["judges"]["combined"]["falseAlarmRate"],
            "undecided": s["undecided"], "endToEndMs": s["endToEndMs"],
        })
    return {"runId": data["runId"], "seeds": data["seeds"], "parallel": data["parallel"], "conditions": rows,
            "reruns": data.get("reruns", [])}


def c2_markdown(s: dict) -> str:
    lines = ["## C2: the Coordinator's decision with one detector broken",
             "",
             f"Run `{s['runId']}`: the C1 journeys ({s['seeds']} per scenario) replayed once per condition, "
             f"{s['parallel']} journeys at a time. Each fault is put on one real detector by a switch in front "
             "of it (`compose.c2.yaml`): *slow* answers after 2 s (the Coordinator waits 500 ms), *dead* fails "
             "every request, *wrong* inverts the real vote (problem ↔ normal), *wrong, silenced* is wrong with "
             "its reliability set to 0 in `detector:weights`. Caught = journeys where the Coordinator raised the "
             "right problem; alerted = journeys with a problem that got any alert (*tracking lost* included); "
             "outage noticed = journeys where the caregiver was told *tracking lost* because detectors failed "
             "or were silenced (every detector for a type down or at reliability 0 for 2 min); false alerts = incidents other than *tracking lost* on "
             "the journeys without a problem; p95 = check-in on the queue to decision recorded.",
             "",
             "| Fault | Detector | " + " | ".join(CASE_NAMES[c] for c in CASES) +
             " | alerted | outage noticed | false alerts | false alarm rate | p95 |",
             "|---|---|" + "---|" * len(CASES) + "---|---|---|---|---|"]
    for r in s["conditions"]:
        caught = [f"{r['caught'][c][0]}/{r['caught'][c][1]}" if c in r["caught"] else "–" for c in CASES]
        p95 = f"{r['endToEndMs']['p95']:.0f} ms" if r["endToEndMs"] else "–"
        lines.append(f"| {r['fault']} | {r['detector'] or 'all healthy'} | " + " | ".join(caught) +
                     f" | {r['alerted'][0]}/{r['alerted'][1]} | {r['outageNoticed'][0]}/{r['outageNoticed'][1]}"
                     f" | {r['falseAlerts']}/{r['journeysWithoutProblem']} "
                     f"| {r['falseAlarmRate']:.1%} | {p95} |")
    undecided = sum(r["undecided"] for r in s["conditions"])
    lines += ["", f"Check-ins not decided in any condition: {undecided}."]
    for r in s.get("reruns", []):
        lines.append(f"Rerun after a Coordinator change: the *{r['fault']}* rows (run `{r['runId']}`).")
    return "\n".join(lines)


def _key(journey: dict) -> tuple:
    return journey["case"], journey["seed"]


def _outcome(row: dict) -> tuple | None:
    """What the caregiver-facing decision was: decision, type, and the types alerted."""
    d = row["decision"]
    if d is None:
        return None
    return d["decision"], d.get("problemType"), tuple(sorted(a["problemType"] for a in d.get("alerts") or []))


def c4(data: dict) -> dict:
    """Each condition against the baseline, journey by journey (same seeds, same check-ins)."""
    conds = {c["condition"]: c for c in data["conditions"]}
    base = {_key(j): j for j in conds["baseline"]["journeys"]} if "baseline" in conds else {}
    rows_out = []
    for c in data["conditions"]:
        rows = [r for j in c["journeys"] for r in j["rows"]]
        changed = extra_incidents = extra_detections = 0
        for j in c["journeys"]:
            b = base.get(_key(j))
            if b is None:
                continue
            changed += sum(_outcome(r) != _outcome(br) for r, br in zip(j["rows"], b["rows"]))
            extra_incidents += max(0, len(j["incidents"]) - len(b["incidents"]))
            extra_detections += (sum(i.get("detectionCount") or 1 for i in j["incidents"])
                                 - sum(i.get("detectionCount") or 1 for i in b["incidents"]))
        delays = [r["endToEndMs"] for r in rows if r["endToEndMs"] is not None]
        rows_out.append({
            "condition": c["condition"],
            "crashes": sum(e["action"] in ("kill", "stop") for e in c["events"]),
            "checkins": len(rows),
            "lost": sum(r["decision"] is None for r in rows),
            "late": sum(bool(r.get("late")) or (r["endToEndMs"] or 0) > 10_000 for r in rows),
            "maxDelayS": round(max(delays) / 1000, 1) if delays else None,
            "decidedTwice": sum(r["decidedCount"] > 1 for r in rows),
            "repeatsSkipped": sum(r["skippedCount"] for r in rows),
            "changedVsBaseline": changed,
            "incidents": sum(len(j["incidents"]) for j in c["journeys"]),
            "extraIncidents": extra_incidents,
            "extraDetections": extra_detections,
            "reportsPosted": c["reportsPosted"],
            "dlqAdded": c["dlqAdded"],
        })
    return {"runId": data["runId"], "seeds": data["seeds"], "parallel": data["parallel"], "conditions": rows_out}


def c4_markdown(s: dict) -> str:
    lines = ["## C4: a Coordinator replica killed, the Alert Service down",
             "",
             f"Run `{s['runId']}`: the C1 journeys ({s['seeds']} per scenario), {s['parallel']} at a time, with 2 "
             "Coordinator replicas on the queue (`compose.c4.yaml`). *replica killed*: every 20 s one replica is "
             "SIGKILLed (alternating) and started 5 s later, as Kubernetes restarts a crashed pod. *alert service "
             "down*: stopped for 45 s. Lost = never decided; late = took over 10 s (a redelivery after the 30 s "
             "visibility timeout); decided twice = a check-in's decision recorded more than once (from both "
             "replicas' logs); repeats skipped = redelivered check-ins the `decided:` marker turned away; changed = "
             "check-ins whose decision or alerts differ from the baseline; extra incidents / detections = what the "
             "Alert Service holds beyond the baseline (a re-sent report updates its incident's detection count).",
             "",
             "| Condition | crashes | check-ins | lost | late | max delay | decided twice | repeats skipped "
             "| changed | incidents (extra) | extra detections | reports posted | dead-lettered |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in s["conditions"]:
        delay = "–" if r["maxDelayS"] is None else f"{r['maxDelayS']:.1f} s"
        lines.append(f"| {r['condition']} | {r['crashes']} | {r['checkins']} | {r['lost']} | {r['late']} | {delay} "
                     f"| {r['decidedTwice']} | {r['repeatsSkipped']} | {r['changedVsBaseline']} "
                     f"| {r['incidents']} ({r['extraIncidents']}) | {r['extraDetections']} | {r['reportsPosted']} "
                     f"| {r['dlqAdded']} |")
    return "\n".join(lines)


def c5(data: dict) -> dict:
    """Each REDUNDANCY_MODE scored like C1 (the Coordinator only), plus its detector calls."""
    modes = {c["mode"]: c for c in data["conditions"]}
    base = {_key(j): j for j in modes["fixed4"]["journeys"]} if "fixed4" in modes else {}
    out = []
    for c in data["conditions"]:
        s = c1({"runId": data["runId"], "seeds": data["seeds"], "journeys": c["journeys"]})
        combined, alerts = s["judges"]["combined"], s["alerts"]
        decided = [r["decision"] for j in c["journeys"] for r in j["rows"] if r["decision"]]
        # Check-ins where fixed4 raised the labelled problem and this mode didn't.
        missed = 0
        for j in c["journeys"]:
            b = base.get(_key(j))
            for r, br in zip(j["rows"], b["rows"] if b else []):
                if br["expected"] in PROBLEMS and br["expected"] in answer(br, "combined")[1]:
                    missed += br["expected"] not in answer(r, "combined")[1]
        out.append({
            "mode": c["mode"],
            "checkins": len(decided),
            "detectorCalls": sum(d["k"] for d in decided),
            "detectorsPerCheckin": round(sum(d["k"] for d in decided) / len(decided), 2) if decided else None,
            "escalatedRate": round(sum(bool(d.get("escalated")) for d in decided) / len(decided), 4) if decided else None,
            "caught": {p: [v["detected"], v["journeys"]] for p, v in combined["problems"].items()},
            "delayS": {p: v["medianDelayS"] for p, v in combined["problems"].items()},
            "problemCheckinsMissedVsFixed4": missed,
            "falseAlarms": combined["falseAlarms"], "falseAlarmRate": combined["falseAlarmRate"],
            "wrongType": combined["wrongType"],
            "alerted": [sum(v["anyAlert"] for v in alerts["cases"].values()),
                        sum(v["journeys"] for v in alerts["cases"].values())],
            "rightType": sum(v["rightType"] for v in alerts["cases"].values()),
            "falseAlerts": alerts["withoutProblem"]["falseAlerts"],
            "journeysWithoutProblem": alerts["withoutProblem"]["journeys"],
            "endToEndMs": s["endToEndMs"],
        })
    return {"runId": data["runId"], "seeds": data["seeds"], "modes": out}


def c5_markdown(s: dict) -> str:
    lines = ["## C5: asking fewer detectors (fixed4 vs adaptive vs fixed2)",
             "",
             f"Run `{s['runId']}`: the C1 journeys ({s['seeds']} per scenario), one at a time, once per "
             "`REDUNDANCY_MODE` (`services/coordinator/app/adaptive.py`). *fixed4* (deployed): all 4 detectors on "
             "every check-in. *adaptive*: 2 at normal risk, the pair that fits the journey step (walking, waiting, "
             "riding), all 4 once risk is elevated or the pair can't decide. *fixed2*: the same pair, more only when "
             "the pair can't decide. Calls = detector calls per check-in and against fixed4; escalated = check-ins "
             "where the pair couldn't decide and the rest were asked; caught and delay as in C1; missed = check-ins "
             "where fixed4 raised the labelled problem and this mode didn't; alerts = journeys with a problem that got "
             "an alert of the right type / any alert; false alerts = alerts on the journeys without a problem.",
             "",
             "| Mode | detectors per check-in | calls vs fixed4 | escalated | "
             + " | ".join(CASE_NAMES[c] for c in CASES)
             + " | missed vs fixed4 | false alarms | wrong type | alerts (right type / any) | false alerts | p95 |",
             "|---|---|---|---|" + "---|" * len(CASES) + "---|---|---|---|---|---|"]
    base_calls = next((m["detectorCalls"] for m in s["modes"] if m["mode"] == "fixed4"), None)
    for m in s["modes"]:
        cells = []
        for c in CASES:
            if c not in m["caught"]:
                cells.append("–")
                continue
            caught, delay = m["caught"][c], m["delayS"][c]
            cells.append(f"{caught[0]}/{caught[1]}" + ("" if delay is None else f", {delay:.0f} s"))
        vs = f"{m['detectorCalls'] / base_calls:.0%}" if base_calls else "–"
        p95 = f"{m['endToEndMs']['p95']:.0f} ms" if m["endToEndMs"] else "–"
        lines.append(f"| **{m['mode']}** | {m['detectorsPerCheckin']} | {vs} | {m['escalatedRate']:.1%} | "
                     + " | ".join(cells)
                     + f" | {m['problemCheckinsMissedVsFixed4']} | {m['falseAlarms']} ({m['falseAlarmRate']:.1%}) "
                       f"| {m['wrongType']} | {m['rightType']} / {m['alerted'][0]} of {m['alerted'][1]} "
                       f"| {m['falseAlerts']}/{m['journeysWithoutProblem']} | {p95} |")
    return "\n".join(lines)


ANALYSES = {"C1": (c1, c1_markdown), "C2": (c2, c2_markdown), "C4": (c4, c4_markdown), "C5": (c5, c5_markdown)}


def newest(experiment: str) -> Path | None:
    """The newest finished run (C2-20261109T061157Z.json), never an unfinished run's progress file."""
    runs = sorted(RESULTS.glob(f"{experiment}-*T*Z.json"))
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
