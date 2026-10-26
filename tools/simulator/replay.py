"""Replay a scenario against live detectors.

Seeds ``trip:{id}:route`` and ``trip:{id}:context`` in Redis, then for each check-in pushes it onto
``trip:{id}:recent`` (LPUSH + LTRIM 10, newest first, like the Check-in Service), builds a
detector-request and POSTs it to every detector in parallel. Needs ``redis``; HTTP is stdlib.
"""

import csv
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from tools.simulator.route import build_route
from tools.simulator.scenarios import INTERVAL_S, generate, strip_labels

RECENT_MAX = 10
# The problem type each detector is responsible for; delay is only scored against that
OWN_PROBLEM = {"route-corridor": "off_route", "heading-speed": "wrong_direction", "dwell-time": "long_dwell"}

Post = Callable[[str, dict], dict]


def post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url.rstrip("/") + "/internal/detect",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def _ask(post: Post, url: str, body: dict) -> dict:
    try:
        return post(url, body)
    except Exception as exc:  # a dead detector must not stop the replay
        return {"detectorId": url, "verdict": "error", "problemType": None, "latencyMs": None, "reason": repr(exc)}


def seed_trip(redis_client, trip_id: str, route: dict, first: dict) -> None:
    redis_client.set(f"trip:{trip_id}:route", json.dumps({"steps": route["steps"], "path": route["path"]}))
    redis_client.delete(f"trip:{trip_id}:recent")
    redis_client.hset(
        f"trip:{trip_id}:context",
        mapping={k: first[k] for k in ("travellerId", "stepIndex", "routeVersion", "status")},
    )


def replay_scenario(
    scenario: str,
    redis_client,
    detector_urls: List[str],
    speed: float = 0,
    seed: int = 42,
    browser: bool = True,
    post: Post = post_json,
    on_row: Optional[Callable[[dict], None]] = None,
) -> List[dict]:
    """One row per check-in: ``{seq, expected, votes: [vote dict per detector, in url order]}``."""
    checkins = generate(scenario, seed, browser=browser)
    trip = checkins[0]["tripId"]
    seed_trip(redis_client, trip, build_route(), checkins[0])
    rows = []
    with ThreadPoolExecutor(max_workers=len(detector_urls)) as pool:
        for c in checkins:
            body = strip_labels(c)
            key = f"trip:{trip}:recent"
            redis_client.lpush(key, json.dumps(body))
            redis_client.ltrim(key, 0, RECENT_MAX - 1)
            redis_client.hset(f"trip:{trip}:context", "stepIndex", c["stepIndex"])
            request = {"requestId": f"{trip}#{c['seq']}", "riskLevel": "normal", "checkin": body}
            votes = list(pool.map(lambda u: _ask(post, u, request), detector_urls))
            row = {"seq": c["seq"], "expected": c["expected"], "votes": votes}
            rows.append(row)
            if on_row:
                on_row(row)
            if speed:
                time.sleep(INTERVAL_S * speed)
    return rows


def _cell(vote: dict) -> str:
    text = vote["verdict"] + (f"/{vote['problemType']}" if vote.get("problemType") else "")
    return f"{text} {vote['latencyMs']:.0f}ms" if vote.get("latencyMs") is not None else text


def format_row(row: dict, widths: List[int]) -> str:
    cells = [f"{row['seq']:>4}", f"{row['expected']:<16}"] + [_cell(v).ljust(w) for v, w in zip(row["votes"], widths)]
    return "  ".join(cells)


def format_header(row: dict, widths: List[int]) -> str:
    names = [v["detectorId"].ljust(w) for v, w in zip(row["votes"], widths)]
    return "  ".join(["seq ", "expected        "] + names)


def write_csv(rows: List[dict], scenario: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{scenario}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    ids = [v["detectorId"] for v in rows[0]["votes"]]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq", "expected"] + [f"{i}_{col}" for i in ids for col in ("verdict", "problemType", "latencyMs")])
        for r in rows:
            w.writerow([r["seq"], r["expected"]] + [x for v in r["votes"] for x in (v["verdict"], v.get("problemType") or "", v.get("latencyMs") if v.get("latencyMs") is not None else "")])
    return path


def summarize(results: Dict[str, List[dict]]) -> Dict[str, dict]:
    """Per detector, over all scenarios.

    ``true_positives``: problem votes whose type equals the check-in's expected label.
    ``false_alarms``: problem votes anywhere else (wrong type, or expected normal/abstain).
    ``delays``: scenario -> check-ins between the first labelled problem check-in and the first
    matching problem vote, only for the problem the detector owns; None when it never fired.
    """
    out: Dict[str, dict] = {}
    for scenario, rows in results.items():
        for i, v in enumerate(rows[0]["votes"]):
            d = out.setdefault(v["detectorId"], {"true_positives": 0, "false_alarms": 0, "abstains": 0, "errors": 0, "delays": {}})
            for r in rows:
                vote = r["votes"][i]
                if vote["verdict"] == "abstain":
                    d["abstains"] += 1
                elif vote["verdict"] == "error":
                    d["errors"] += 1
                elif vote["verdict"] == "problem":
                    d["true_positives" if vote["problemType"] == r["expected"] else "false_alarms"] += 1
            own = OWN_PROBLEM.get(v["detectorId"])
            labelled = [n for n, r in enumerate(rows) if r["expected"] == own]
            if own and labelled:
                fired = [n for n, r in enumerate(rows) if r["votes"][i]["problemType"] == own and r["expected"] == own]
                d["delays"][scenario] = fired[0] - labelled[0] if fired else None
    return out


def format_summary(summary: Dict[str, dict]) -> str:
    lines = [f"{'detector':<16} {'TP':>5} {'false alarms':>13} {'abstains':>9} {'errors':>7}  delay (check-ins)"]
    for name, d in summary.items():
        delay = ", ".join(f"{s}: {'missed' if n is None else n}" for s, n in d["delays"].items()) or "-"
        lines.append(f"{name:<16} {d['true_positives']:>5} {d['false_alarms']:>13} {d['abstains']:>9} {d['errors']:>7}  {delay}")
    return "\n".join(lines)
