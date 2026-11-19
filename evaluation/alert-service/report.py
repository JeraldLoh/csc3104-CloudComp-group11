"""Writes RESULTS.md from the newest results/A1-*.json and results/A2-*.json (run.py). Every number in it comes
from those files.

    python evaluation/alert-service/report.py
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"


def newest(experiment: str) -> dict | None:
    files = sorted(RESULTS.glob(f"{experiment}-*.json"))
    return json.loads(files[-1].read_text()) if files else None


def ms(p: dict | None, key: str = "p95") -> str:
    return "–" if not p else f"{p[key]:.0f} ms"


def s(p: dict | None, key: str) -> str:
    return "–" if not p else f"{p[key]:+.1f} s"


OUTBOX_TERMS = (
    "*conditional + outbox* is as deployed: every write that must happen once (creating an incident, claiming a "
    "reminder, claiming a push) is a DynamoDB conditional write, and each push is kept on its incident until sent, so "
    "another replica sends it if the one that should have died. *read-then-write*: those writes read the incident, "
    "decide, then write without a condition. *no outbox*: the design before the outbox, where a replica pushes "
    "straight away and a push it didn't send is lost.")


def a1_section(run: dict) -> list[str]:
    first = run["conditions"][0]
    lines = [
        "## A1: the same problem reported many times at once",
        "",
        f"Run `{run['runId']}`{' (--quick)' if run['quick'] else ''}: {first['problems']} problems, each reported "
        f"{first['reportsSent'] // first['problems']} times at once, the copies spread over the replicas in turn. "
        f"{OUTBOX_TERMS} "
        "*killed*: one replica killed (no shutdown) and started again a few seconds later, taking turns; a report that "
        "got no answer is sent again to the next replica. Expected per problem: 1 incident, 1 \"created\" answer, "
        "1 push, and a detection count equal to the reports accepted.",
        "",
        "| Condition | kills | reports | failed | incidents | \"created\" answers (extra) | pushes (duplicate / missing) "
        "| pushes taken over | detections recorded (lost / extra) | request p50 / p95 | first push p95 / max |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in run["conditions"]:
        lines.append(
            f"| {c['name']} | {c['kills']} | {c['reportsSent']} | {c['failed']} | {c['incidents']} "
            f"| {c['createdAnswers']} ({c['extraCreated']}) | {c['pushes']} ({c['duplicatePushes']} / {c['missingPushes']}) "
            f"| {'–' if c.get('takenOver') is None else c['takenOver']} "
            f"| {c['detectionsRecorded']} ({c['lostDetections']} / {c['extraDetections']}) "
            f"| {ms(c['requestMs'], 'p50')} / {ms(c['requestMs'])} | {ms(c['firstPushMs'])} / {ms(c['firstPushMs'], 'max')} |")
    lines += ["",
              "*Lost* detections are reports accepted but not counted: two writers read the same count, or a "
              "racing create reset it to 1. *Extra* detections are reports saved whose answer was lost to a crash, "
              "so they were sent again (at-least-once delivery), which only raises the count. A *missing* push is a "
              "problem whose incident exists but no push was ever sent. *Taken over*: pushes the replica that saved "
              "the alert didn't send (it was killed first), sent by another replica once their lease ran out "
              "(`PUSH_LEASE_S`, 15 s); they are why the slowest first push takes about 15–20 s.", ""]
    return lines


def a2_section(run: dict) -> list[str]:
    first = run["conditions"][0]
    lines = [
        "## A2: reminders with several replicas checking",
        "",
        f"Run `{run['runId']}`{' (--quick)' if run['quick'] else ''}: {first['alerts']} urgent alerts nobody "
        f"acknowledges. Every replica looks for due reminders every {first['checkS']:g} s; a reminder is due "
        f"{first['afterS']} s after the alert before it, up to {first['max']} (deployed: every 15 s, 120 s, 3). "
        f"{OUTBOX_TERMS} Here the conditional write that matters is the reminder's claim on `escalationLevel`. "
        f"The push lease is scaled like the reminders ({first.get('leaseS', '–')} s; deployed 15 s). "
        f"Expected: exactly {first['max']} reminders per alert. *Late* is each reminder's gap from the "
        "alert before it minus the wait, so up to one check interval by design (timestamps are kept to the second, "
        "so it can be slightly negative).",
        "",
        "| Condition | kills | reminders sent / expected | extra (alerts) | missing (alerts) | pushes taken over "
        "| at the last level | late p50 / p95 / max |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in run["conditions"]:
        late = c["lateS"]
        lines.append(
            f"| {c['name']} | {c['kills']} | {c['reminders']} / {c['expectedReminders']} "
            f"| {c['extraReminders']} ({c['alertsWithExtra']}) | {c['missingReminders']} ({c['alertsWithMissing']}) "
            f"| {'–' if c.get('takenOver') is None else c['takenOver']} "
            f"| {c['atFinalLevel']} / {c['alerts']} | {s(late, 'p50')} / {s(late, 'p95')} / {s(late, 'max')} |")
    lines.append("")
    return lines


def main() -> None:
    lines = ["# Alert Service results (A1 and A2)", "",
             "Written by `report.py` from `results/`. How the experiments work: [README.md](README.md).", ""]
    found = False
    for experiment, section in (("A1", a1_section), ("A2", a2_section)):
        run = newest(experiment)
        if run:
            lines += section(run)
            found = True
    if not found:
        raise SystemExit("no results yet: run run.py first")
    (HERE / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {HERE / 'RESULTS.md'}")


if __name__ == "__main__":
    main()
