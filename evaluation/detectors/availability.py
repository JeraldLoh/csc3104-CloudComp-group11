# E9 availability, real faults in the local kind cluster while the probe calls every detector
#   python evaluation/detectors/availability.py        from the repo root, needs kubectl on the kind cluster
# it crashes one route-corridor pod, then both dwell-time pods, then stops redis for 30 s
# redis keeps nothing on disk locally so its live trip data is lost, never run this during a demo

import argparse
import json
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NS = "csc3104"
JOB = "detector-probe"
DETECTORS = ["detector-route", "detector-heading", "detector-dwell", "detector-progress"]
WATCHED = DETECTORS + ["cloud-redis"]
# seconds after the probe starts, name, what to do
FAULTS = [
    (30, "one route-corridor pod crashes", ["delete", "pod", "{one_route_pod}", "--grace-period=0", "--force"]),
    (100, "both dwell-time pods crash", ["delete", "pod", "-l", "app=detector-dwell", "--grace-period=0", "--force"]),
    (180, "redis stops", ["scale", "deployment/cloud-redis", "--replicas=0"]),
    (210, "redis starts again", ["scale", "deployment/cloud-redis", "--replicas=1"])
]
DURATION_S = 300
JOB_YAML = """
apiVersion: batch/v1
kind: Job
metadata: {{ name: {job}, namespace: {ns} }}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: probe
          image: cloud-simulator:local
          imagePullPolicy: IfNotPresent
          args: ["probe", "--rate=10", "--duration={duration}", "--redis=redis://cloud-redis:6379/0",
                 "--detectors={urls}"]
          env: [{{ name: PYTHONUNBUFFERED, value: "1" }}]
"""


def kubectl(*args, check=True):
    return subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True, check=check).stdout


# ready replicas of every watched deployment
def ready():
    items = json.loads(kubectl("get", "deployments", "-o", "json"))["items"]
    return {d["metadata"]["name"]: d["status"].get("readyReplicas", 0) for d in items if d["metadata"]["name"] in WATCHED}


# start the probe job and return its start time from its first log line
def start_probe():
    kubectl("delete", "job", JOB, "--ignore-not-found", "--wait=true")
    urls = ",".join(f"http://{d}:8000" for d in DETECTORS)
    manifest = JOB_YAML.format(job=JOB, ns=NS, duration=DURATION_S, urls=urls)
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, text=True, check=True, capture_output=True)
    for _ in range(120):
        out = kubectl("logs", f"job/{JOB}", check=False)
        if out.startswith("{"):
            return json.loads(out.splitlines()[0])["start"]
        time.sleep(1)
    raise SystemExit("the probe did not start, see kubectl get pods -n csc3104")


# inject the faults on time and sample ready replicas every second until the probe ends
def run_faults(start):
    samples, events, pending = [], [], list(FAULTS)
    while time.time() < start + DURATION_S + 5:
        t = time.time() - start
        while pending and t >= pending[0][0]:
            _, name, args = pending.pop(0)
            pod = kubectl("get", "pods", "-l", "app=detector-route", "-o", "jsonpath={.items[0].metadata.name}")
            kubectl(*[a.format(one_route_pod=pod) for a in args])
            events.append({"t": round(time.time() - start, 1), "fault": name})
            print(f"{events[-1]['t']:6.1f} s  {name}", flush=True)
        samples.append({"t": round(time.time() - start, 1), **ready()})
        time.sleep(max(0.0, 1 - (time.time() - start - t)))
    return samples, events


# first sample at or after since where cond holds, as seconds after since
def recovered_after(samples, since, cond):
    hit = next((s for s in samples if s["t"] >= since and cond(s)), None)
    return None if hit is None else round(hit["t"] - since, 1)


# share of calls in a window answered in budget and as real votes, overall and per detector
def window(lines, a, b, url=None):
    rows = [ln if url is None else ln["byDetector"][url] for ln in lines if a <= ln["t"] < b]
    sent = sum(r["sent"] for r in rows)
    pct = (lambda k: round(100 * sum(r[k] for r in rows) / sent, 2)) if sent else (lambda k: None)
    return {"sent": sent, "answeredInBudgetPct": pct("inBudget"), "realVotesPct": pct("votes"),
            "errors": sum(r["errors"] for r in rows)}


def summarise(lines, samples, events):
    at = {e["fault"]: e["t"] for e in events}
    url = {d: f"http://{d}:8000" for d in DETECTORS}
    kill_one, kill_all = at["one route-corridor pod crashes"], at["both dwell-time pods crash"]
    redis_down, redis_up = at["redis stops"], at["redis starts again"]
    all_ready = lambda s: all(s.get(d, 0) >= 2 for d in DETECTORS)  # noqa: E731
    return {
        "baseline": window(lines, 0, kill_one),
        "oneRoutePod": {
            "routeCorridor": window(lines, kill_one, kill_one + 30, url["detector-route"]),
            "backToTwoReadyS": recovered_after(samples, kill_one + 1, lambda s: s["detector-route"] >= 2)
        },
        "bothDwellPods": {
            "dwellTime": window(lines, kill_all, kill_all + 30, url["detector-dwell"]),
            "others": {d: window(lines, kill_all, kill_all + 30, url[d]) for d in DETECTORS if d != "detector-dwell"},
            "firstReadyS": recovered_after(samples, kill_all, lambda s: s["detector-dwell"] >= 1),
            "backToTwoReadyS": recovered_after(samples, kill_all, lambda s: s["detector-dwell"] >= 2)
        },
        "redisOutage": {
            "during": window(lines, redis_down, redis_up),
            "detectorsAllReadyAfterRedisBackS": recovered_after(samples, redis_up, all_ready),
            "after": window(lines, redis_up + 30, DURATION_S)
        },
        "podRestarts": {d: kubectl("get", "pods", "-l", f"app={d}", "-o",
                                   "jsonpath={range .items[*]}{.status.containerStatuses[0].restartCount} {end}").split()
                        for d in DETECTORS}
    }


# sent and real votes per second and detector, late answers can arrive as a second line for the same second
def per_second(lines, url):
    totals = {}
    for ln in lines:
        sent, votes = totals.get(ln["t"], (0, 0))
        r = ln["byDetector"][url]
        totals[ln["t"]] = (sent + r["sent"], votes + r["votes"])
    return sorted(totals.items())


# answered share per second with the faults marked, and ready replicas below
def chart(lines, samples, events, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(10, 6), sharex=True, height_ratios=[3, 2])
    for d in DETECTORS:
        rows = per_second(lines, f"http://{d}:8000")
        top.plot([t for t, _ in rows], [100 * v / s if s else 0 for _, (s, v) in rows], label=d, linewidth=1.2)
    top.set_ylabel("% of calls answered with a real vote")
    top.set_ylim(-3, 103)
    top.legend(fontsize=8, ncols=4, loc="lower left")
    for name in [d for d in DETECTORS if d in ("detector-route", "detector-dwell")] + ["cloud-redis"]:
        bottom.step([s["t"] for s in samples], [s.get(name, 0) for s in samples], where="post", label=name)
    bottom.set_ylabel("ready replicas")
    bottom.set_xlabel("seconds since the probe started")
    bottom.legend(fontsize=8, ncols=3, loc="lower right")
    for ax in (top, bottom):
        ax.grid(alpha=0.3)
        for e in events:
            ax.axvline(e["t"], color="red", linestyle="--", linewidth=0.8)
    for e in events:
        top.annotate(e["fault"], (e["t"], 101), fontsize=7, rotation=90, va="top", ha="right")
    top.set_title("E9: detector availability under real faults (10 calls/s to each detector)")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=HERE / "results")
    ap.add_argument("--chart-only", action="store_true", help="redraw from results/e9-availability.json")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    saved = args.out / "e9-availability.json"
    if args.chart_only:
        data = json.loads(saved.read_text())
        chart(data["lines"], data["samples"], data["events"], args.out / "e9-availability.png")
        return

    start = start_probe()
    print(f"probe running, faults at {[f[0] for f in FAULTS]} s, {DURATION_S} s in total", flush=True)
    samples, events = run_faults(start)
    kubectl("wait", "--for=condition=complete", f"job/{JOB}", "--timeout=120s", check=False)
    lines = [json.loads(ln) for ln in kubectl("logs", f"job/{JOB}").splitlines() if ln.startswith('{"t"')]
    summary = summarise(lines, samples, events)
    saved.write_text(json.dumps({"summary": summary, "events": events, "samples": samples, "lines": lines}, indent=1) + "\n")
    chart(lines, samples, events, args.out / "e9-availability.png")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
