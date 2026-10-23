"""Turns evaluation/results/*.json (from run.py) into numbers: results/summary.json and
RESULTS.md. Every number in RESULTS.md is computed here from the raw run files.

    python evaluation/analyse.py
"""
import glob
import json
import math
import statistics as st
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
SCALE = 10  # must match run.py; converts scaled runs back to real-world time


def pct(n, d):
    return 100.0 * n / d if d else 0.0


def quantile(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    i = (len(xs) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def load(name):
    return json.loads((RESULTS / f"{name}.json").read_text())


# ----------------------------------------------------------------------------- one run

def core(d) -> dict:
    """Ground truth for one run: what was made, stored, queued, lost, duplicated, ordered."""
    t = d["truth"]
    stored = {}
    for x in t["stored"]:
        stored.setdefault((x["tripId"], x["seq"]), x)
    msgs = Counter((m["tripId"], m["seq"]) for m in t["messages"])
    alive = [p for p in d["phones"] if not p["dead"]]
    made_alive = [(p["tripId"], g["seq"]) for p in alive for g in p["generated"]]
    made_all = [(p["tripId"], g["seq"]) for p in d["phones"] for g in p["generated"]]
    left = sum(p["leftInOutbox"] for p in alive)
    lost = [k for k in made_alive if k not in stored]

    # Order: after the run, each trip's live context must point at its newest stored
    # check-in, and its recent list must be the newest 10, newest first.
    order_bad = 0
    by_trip = {}
    for (trip, seq) in stored:
        by_trip.setdefault(trip, []).append(seq)
    for trip, seqs in by_trip.items():
        r = t["redis"].get(trip)
        if not r:
            continue
        top = sorted(seqs, reverse=True)
        if r["lastseq"] != top[0] or r["recent"] != top[: len(r["recent"])]:
            order_bad += 1

    # Delay from the phone making a check-in to the server accepting it (phone clock).
    out = d["config"].get("outages") or []
    during, normal = [], []
    for p in d["phones"]:
        for g in p["generated"]:
            a = p["acked"].get(str(g["seq"]))
            if a is None:
                continue
            delay = (a - g["t"]) / 1000
            (during if any(o["startMs"] <= g["t"] < o["endMs"] for o in out) else normal).append(delay)

    attempts = d["attempts"]
    return {
        "phones": len(d["phones"]),
        "made": len(made_all),
        "made_by_live_phones": len(made_alive),
        "stored": len(stored),
        "lost": len(lost),
        "lost_pct": pct(len(lost), len(made_alive)),
        "still_on_phone_at_end": left,
        "stuck_on_server": sum(1 for x in stored.values() if not x["enqueued"]),
        "queued_unique": len(msgs),
        "queue_extra_copies": sum(msgs.values()) - len(msgs),
        "queue_extra_copies_pct": pct(sum(msgs.values()) - len(msgs), len(msgs)),
        "trips_out_of_order": order_bad,
        "requests": len(attempts),
        "requests_per_checkin": len(attempts) / max(1, len(made_all)),
        "outcomes": dict(Counter(f"{a['outcome']}:{a['reason'] or ''}".rstrip(":") for a in attempts)),
        "delay_s_normal_p50": quantile(normal, 0.5),
        "delay_s_normal_p95": quantile(normal, 0.95),
        "delay_s_outage_p50": quantile(during, 0.5),
        "delay_s_outage_max": max(during) if during else None,
    }


def mean_range(rows, key):
    xs = [r[key] for r in rows if r.get(key) is not None]
    if not xs:
        return None
    return {"mean": st.mean(xs), "min": min(xs), "max": max(xs), "n": len(xs)}


# ----------------------------------------------------------------------------- E2 timeline

def e2_timeline(d, bin_ms=500) -> dict:
    reconnect = d["config"]["outages"][0]["endMs"]
    total = sum(len(p["generated"]) for p in d["phones"])
    acks = sorted(a for p in d["phones"] for a in p["acked"].values())
    reqs = [a for a in d["attempts"] if a["t0"] >= reconnect - 50]
    end = max([a["t1"] for a in reqs] + acks + [reconnect]) - reconnect
    nbins = int(end // bin_ms) + 2
    offered, shed = [0] * nbins, [0] * nbins
    for a in reqs:
        b = int(max(0, a["t0"] - reconnect) // bin_ms)
        offered[b] += 1
        if a["reason"] == "HTTP 503":
            shed[b] += 1
    delivered, i = [], 0
    for b in range(nbins):
        while i < len(acks) and acks[i] - reconnect <= (b + 1) * bin_ms:
            i += 1
        delivered.append(pct(i, total))
    last = (acks[-1] - reconnect) / 1000 if acks else None
    return {
        "bin_s": bin_ms / 1000,
        "offered_per_s": [x * 1000 / bin_ms for x in offered],
        "shed_per_s": [x * 1000 / bin_ms for x in shed],
        "delivered_pct": delivered,
        "all_delivered": len(acks) == total,
        "delivered_pct_end": pct(len(acks), total),
        "seconds_to_deliver_all": last if len(acks) == total else None,
        "seconds_to_deliver_95": next(((b + 1) * bin_ms / 1000 for b, v in enumerate(delivered) if v >= 95), None),
        "requests_after_reconnect": len(reqs),
        "shed_after_reconnect": sum(1 for a in reqs if a["reason"] == "HTTP 503"),
        "peak_offered_per_s": max(x * 1000 / bin_ms for x in offered),
    }


# ----------------------------------------------------------------------------- E3

def e3_series(d) -> dict:
    t0 = d["startedAtEpochMs"]
    samples = [{"t_s": (s["epochMs"] - t0) / 1000, "stuck": s["stuck"],
                "leader": (s["leader"] or "").rsplit("-", 1)[0] or None} for s in d["samples"]]
    killed = d.get("killed")
    takeover = None
    if killed:
        k = (killed["epochMs"] - t0) / 1000
        dead = f"replica-{killed['replica']}"
        nxt = next((s for s in samples if s["t_s"] > k and s["leader"] and s["leader"] != dead), None)
        takeover = (nxt["t_s"] - k) if nxt else None
        killed = {"t_s": k, "replica": dead}
    return {"samples": samples, "killed": killed, "takeover_s": takeover,
            "stuck_peak": max((s["stuck"] for s in samples), default=0),
            "stuck_end": samples[-1]["stuck"] if samples else None}


# ----------------------------------------------------------------------------- E4 (phi accrual)

PHI_THRESHOLD = 8.0
MIN_STD_S = 0.5     # floor on the spread, as in Akka's phi accrual implementation
WINDOW = 100        # same size as hb:{tripId}:intervals


def phi_detection_time(intervals_s: list[float]) -> float | None:
    """Seconds after the last heartbeat at which phi reaches the threshold
    (Hayashibara et al. 2004, normal model): phi(t) = -log10(1 - F(t))."""
    xs = intervals_s[-WINDOW:]
    if len(xs) < 2:
        return None
    mu, sd = st.mean(xs), max(st.pstdev(xs), MIN_STD_S)
    z = st.NormalDist().inv_cdf(1 - 10 ** -PHI_THRESHOLD)
    return mu + z * sd


def e4(d) -> dict:
    """How fast would the Heartbeat Monitor notice a phone that dies right after it
    caught up from the 5-minute outage?
      design   the intervals the service actually recorded (hb:{trip}:intervals in Redis)
      naive    if every arriving check-in counted as a heartbeat (batches included)
      before   the interval history up to the outage (normal rhythm), for reference"""
    out = d["config"]["outages"][0]
    rows = []
    for p in d["phones"]:
        r = d["truth"]["redis"].get(p["tripId"])
        if not r or not r["intervals"]:
            continue
        design = [x / 1000 for x in reversed(r["intervals"])]  # Redis list is newest first
        arrivals = sorted(p["acked"].values())                 # every check-in, batches included
        naive = [(b - a) / 1000 for a, b in zip(arrivals, arrivals[1:])]
        before = [(b - a) / 1000 for a, b in zip(arrivals, arrivals[1:]) if b < out["startMs"]]
        rows.append({"design": phi_detection_time(design), "naive": phi_detection_time(naive),
                     "before": phi_detection_time(before),
                     "zero_gaps_naive": sum(1 for x in naive[-WINDOW:] if x < 0.05)})
    def med(k):
        xs = [r[k] for r in rows if r[k] is not None]
        return {"median": st.median(xs), "min": min(xs), "max": max(xs)} if xs else None
    return {"phones": len(rows), "detect_s_design": med("design"), "detect_s_naive": med("naive"),
            "detect_s_before_outage": med("before"), "zero_gaps_naive_median": st.median(r["zero_gaps_naive"] for r in rows),
            "threshold": PHI_THRESHOLD, "min_std_s": MIN_STD_S, "window": WINDOW}


# ----------------------------------------------------------------------------- all

def main() -> None:
    names = sorted(Path(p).stem for p in glob.glob(str(RESULTS / "*.json")) if not p.endswith("summary.json"))
    summary = {"runs": {}, "E1": {}, "E2": {}, "E3": {}}
    for n in names:
        if n.startswith("smoke"):
            continue
        summary["runs"][n] = core(load(n))

    # E1 grouped by outage length and app kind, mean/min/max over repeats
    for n, row in summary["runs"].items():
        if n.startswith("E1-") and "min-" in n and "real" not in n:
            minutes, kind = n.split("-")[1], n.split("-")[2]
            summary["E1"].setdefault(f"{minutes}-{kind}", []).append(row)
    summary["E1"] = {k: {m: mean_range(rows, m) for m in ("lost_pct", "queue_extra_copies_pct", "trips_out_of_order",
                                                         "stuck_on_server", "still_on_phone_at_end", "requests_per_checkin",
                                                         "delay_s_outage_p50", "delay_s_outage_max", "delay_s_normal_p95")}
                     for k, rows in summary["E1"].items()}
    for v in ("lockstep", "jitter-retries", "full"):
        if (RESULTS / f"E2-{v}.json").exists():
            summary["E2"][v] = e2_timeline(load(f"E2-{v}"))
    for v in ("off", "on", "on-leader-killed"):
        if (RESULTS / f"E3-cleanup-{v}.json").exists():
            summary["E3"][v] = e3_series(load(f"E3-cleanup-{v}"))
    if (RESULTS / "E1-real-5min-outbox.json").exists():
        summary["E4"] = e4(load("E1-real-5min-outbox"))
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=1))
    print(f"analysed {len(summary['runs'])} runs -> {(RESULTS / 'summary.json').relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
