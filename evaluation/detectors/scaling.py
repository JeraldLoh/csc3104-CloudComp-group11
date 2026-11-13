# E8 figure from an in-cluster load test, replicas per detector and p95 latency over time
#   python evaluation/detectors/scaling.py evaluation/detectors/results/e8-stress-hpa.csv evaluation/detectors/results/e8-stress.log
# the csv is t_s,hpa,cpu,replicas sampled every 15 s and the log is the load job output

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

# one progress line of the load job, seconds since start and the window p95
PROGRESS = re.compile(r"^\s*(\d+)s\s+[\d.]+ req/s\s+p50\s+[\d.]+ ms\s+p95\s+([\d.]+) ms")


# replicas and cpu over time per autoscaler
def read_hpa(path):
    series = defaultdict(lambda: {"t": [], "replicas": [], "cpu": []})
    with open(path) as f:
        for row in csv.DictReader(f):
            if not row["replicas"]:
                continue
            s = series[row["hpa"]]
            s["t"].append(int(row["t_s"]))
            s["replicas"].append(int(row["replicas"]))
            s["cpu"].append(int(row["cpu"]) if row["cpu"] else None)
    return series


# p95 per 10 s window from the load job log
def read_p95(path):
    points = [PROGRESS.match(line) for line in Path(path).read_text().splitlines()]
    return [(int(m[1]), float(m[2])) for m in points if m]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hpa_csv", type=Path)
    ap.add_argument("load_log", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--title", default="E8: detectors under 400 requests/s")
    args = ap.parse_args()

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    hpa, p95 = read_hpa(args.hpa_csv), read_p95(args.load_log)
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    # shade while the load ran
    for ax in (top, bottom):
        ax.axvspan(0, p95[-1][0], color="grey", alpha=0.12, label="load running" if ax is top else None)
    for name, s in sorted(hpa.items()):
        top.step(s["t"], s["replicas"], where="post", label=name)
    top.set_ylabel("replicas")
    top.set_ylim(0, 5)
    top.legend(fontsize=8, ncols=4, loc="lower right")
    top.grid(alpha=0.3)
    top.set_title(args.title)
    bottom.plot([t for t, _ in p95], [v for _, v in p95], marker=".")
    bottom.axhline(500, color="red", linestyle="--", linewidth=1, label="500 ms Coordinator budget")
    bottom.set_yscale("log")
    bottom.set_ylabel("p95 latency, ms")
    bottom.set_xlabel("seconds since the load started")
    bottom.legend(fontsize=8)
    bottom.grid(alpha=0.3)
    fig.tight_layout()
    out = args.out or args.hpa_csv.with_suffix(".png")
    fig.savefig(out, dpi=150)
    print(f"-> {out}")


if __name__ == "__main__":
    main()
