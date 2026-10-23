"""Figures for the article and the video, from evaluation/results/ (run analyse.py first).

    python evaluation/plot.py        ->  evaluation/figures/*.pdf (article) and *.png (slides)

Sized for one IEEE column (3.5 in). Colours keep the same meaning in every figure:
blue = CareRoute's design, orange = the baseline it is compared with, aqua = an
in-between variant. Every series is also told apart by line style or marker and
labelled directly, so the figures still work in greyscale print.
"""
import json
import statistics as st
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
RESULTS, FIGS = HERE / "results", HERE / "figures"
SCALE = 10

DESIGN, BASELINE, MIDDLE = "#2a78d6", "#eb6834", "#1baf7a"   # validated palette slots 1-3
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 7.5, "axes.titlesize": 8, "axes.labelsize": 7.5,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
    "lines.linewidth": 1.5, "lines.solid_capstyle": "round", "savefig.dpi": 300,
    "figure.facecolor": "white", "axes.facecolor": "white",
})


def save(fig, name):
    FIGS.mkdir(exist_ok=True)
    if not fig.legends:  # figures with a shared legend lay themselves out
        fig.tight_layout(pad=0.4)
    for ext in ("pdf", "png"):
        fig.savefig(FIGS / f"{name}.{ext}", bbox_inches="tight")
    plt.close(fig)
    print("  figures/" + name + ".pdf/.png")


def load(name):
    p = RESULTS / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


# ----------------------------------------------------------------------------- E1

def fig_e1_loss(summary):
    groups = sorted({k.split("-")[0] for k in summary["E1"]}, key=lambda m: int(m.replace("min", "")))
    if not groups:
        return
    fig, ax = plt.subplots(figsize=(3.5, 2.1))
    w = 0.32
    for j, (kind, colour, label) in enumerate((("outbox", DESIGN, "CareRoute app (outbox)"),
                                               ("naive", BASELINE, "Send once, no outbox"))):
        xs, ys, lo, hi = [], [], [], []
        for i, g in enumerate(groups):
            r = summary["E1"].get(f"{g}-{kind}", {}).get("lost_pct")
            if not r:
                continue
            xs.append(i + (j - 0.5) * (w + 0.04))
            ys.append(r["mean"])
            lo.append(r["mean"] - r["min"])
            hi.append(r["max"] - r["mean"])
        ax.bar(xs, ys, width=w, color=colour, label=label, yerr=[lo, hi],
               error_kw={"elinewidth": 0.8, "ecolor": INK2, "capsize": 2})
        for x, y in zip(xs, ys):
            ax.text(x, y + 1.2, f"{y:.0f}%" if y >= 1 or y == 0 else f"{y:.1f}%", ha="center", va="bottom", color=INK, fontsize=7)
    ax.set_xticks(range(len(groups)), [f"{g.replace('min', '')} min" for g in groups])
    ax.set_xlabel("Length of the signal loss")
    ax.set_ylabel("Check-ins lost (%)")
    ax.set_ylim(0, max(60, ax.get_ylim()[1] * 1.15))
    ax.legend(loc="upper left", fontsize=7)
    ax.grid(axis="x", visible=False)
    save(fig, "e1-loss")


def cumulative_acked(d, since_ms):
    total = sum(len(p["generated"]) for p in d["phones"] if not p["dead"])
    acks = sorted(a for p in d["phones"] for a in p["acked"].values() if a >= since_ms)
    already = sum(1 for p in d["phones"] for a in p["acked"].values() if a < since_ms)
    xs, ys = [0.0], [100 * already / total]
    for i, a in enumerate(acks, 1):
        xs.append((a - since_ms) / 1000 * SCALE)
        ys.append(100 * (already + i) / total)
    return xs, ys, total


def fig_e1_budget():
    off, on = load("E1-batch-budget-off"), load("E1-batch-budget-on")
    if not (off and on):
        return
    end = off["config"]["outages"][0]["endMs"]
    fig, ax = plt.subplots(figsize=(3.5, 2.1))
    last_x = 0
    for d, colour, style, label in ((on, DESIGN, "-", "Batches time-limited (1 s)"),
                                    (off, BASELINE, "--", "No time limit")):
        xs, ys, _ = cumulative_acked(d, end)
        xs.append((d["endedAtMs"] - end) / 1000 * SCALE)
        ys.append(ys[-1])
        last_x = max(last_x, xs[-1])
        ax.step(xs, ys, where="post", color=colour, linestyle=style, linewidth=1.8)
        ax.text(xs[-1], ys[-1] + (3 if d is on else -3), f"{label}: {ys[-1]:.0f}%", ha="right",
                va="bottom" if d is on else "top", color=INK, fontsize=7)
    ax.set_xlim(0, last_x)
    ax.set_ylim(0, 105)
    ax.set_xlabel("Seconds after the signal returns")
    ax.set_ylabel("Check-ins acknowledged (%)")
    save(fig, "e1-retry-storm")


# ----------------------------------------------------------------------------- E2

def fig_e2(summary):
    e2 = summary.get("E2") or {}
    if not e2:
        return
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(3.5, 3.7), sharex=True)
    handles = []
    series = (("lockstep", BASELINE, "--", "No randomness (lockstep)"),
              ("jitter-retries", MIDDLE, ":", "Random retry delays"),
              ("full", DESIGN, "-", "Random retry delays + spread reconnect"))
    xmax = 0
    for key, colour, style, label in series:
        v = e2.get(key)
        if not v:
            continue
        xs = [i * v["bin_s"] for i in range(len(v["offered_per_s"]))]
        xmax = max(xmax, xs[-1])
        if key != "jitter-retries":  # (a) shows the two extremes; the middle one sits between
            a1.plot(xs, v["offered_per_s"], color=colour, linestyle=style, linewidth=1.2)
        (h,) = a2.plot(xs, v["delivered_pct"], color=colour, linestyle=style, linewidth=1.8, label=label)
        handles.append(h)
        phones = v.get("phones", "all")
    a1.set_ylabel("Requests per second")
    a1.set_title("(a) Load on the server", loc="left", color=INK)
    a2.set_ylabel("Backlog delivered (%)")
    a2.set_xlabel(f"Seconds after {phones} phones regain signal at once")
    a2.set_title("(b) Progress", loc="left", color=INK)
    a2.set_ylim(0, 105)
    a2.set_xlim(0, xmax)
    fig.legend(handles=handles, loc="upper center", ncol=1, fontsize=6.5, bbox_to_anchor=(0.55, 1.0))
    fig.tight_layout(pad=0.4, rect=(0, 0, 1, 0.86))
    save(fig, "e2-reconnect-storm")


# ----------------------------------------------------------------------------- E3

def fig_e3(summary):
    """Small multiples, one run per panel: three noisy lines on one axis are unreadable."""
    e3 = summary.get("E3") or {}
    runs = [(k, c, st_, lab) for k, c, st_, lab in
            (("off", BASELINE, "--", "Clean-up task off"),
             ("on", DESIGN, "-", "Clean-up task on"),
             ("on-leader-killed", MIDDLE, "-", "Clean-up on, leader replica killed"))
            if e3.get(k) and e3[k]["samples"]]
    if not runs:
        return
    top = max(s["stuck"] for k, *_ in runs for s in e3[k]["samples"])
    xmax = max(s["t_s"] * SCALE for k, *_ in runs for s in e3[k]["samples"])
    fig, axes = plt.subplots(len(runs), 1, figsize=(3.5, 0.95 * len(runs) + 0.45), sharex=True, sharey=True)
    for ax, (key, colour, style, label) in zip(axes, runs):
        v = e3[key]
        pts = [(s["t_s"] * SCALE, s["stuck"]) for s in v["samples"] if s["t_s"] >= 0]
        ax.step([x for x, _ in pts], [y for _, y in pts], where="post", color=colour, linestyle=style, linewidth=1.0)
        ax.text(0.01, 0.93, label, transform=ax.transAxes, ha="left", va="top", color=INK, fontsize=7)
        end = pts[-1][1]
        ax.text(0.99, 0.93, f"stuck at the end: {end}", transform=ax.transAxes, ha="right", va="top",
                color=INK, fontsize=7, fontweight="bold" if end else "normal")
        if v.get("killed"):
            kx = v["killed"]["t_s"] * SCALE
            ax.axvline(kx, color=INK2, linewidth=0.8, linestyle=(0, (2, 2)))
            took = v.get("takeover_s")
            note = f"leader killed; new leader after {took * SCALE:.0f} s" if took is not None else "leader killed"
            ax.text(kx + 4, 0.7, note, color=INK2, fontsize=6.5, ha="left", va="center", transform=ax.get_xaxis_transform())
        ax.set_ylim(0, top * 1.45 + 0.5)
        ax.set_xlim(0, xmax)
        ax.grid(axis="x", visible=False)
    axes[len(axes) // 2].set_ylabel("Check-ins stuck\n(stored, not queued)")
    axes[-1].set_xlabel("Seconds (real-world equivalent)")
    save(fig, "e3-cleanup")


# ----------------------------------------------------------------------------- E4

def fig_e4():
    d = load("E1-real-5min-outbox")
    if not d:
        return
    import sys
    sys.path.insert(0, str(HERE))
    from analyse import MIN_STD_S, PHI_THRESHOLD, WINDOW
    # A representative phone: the median detection time under the design rule.
    rows = []
    for p in d["phones"]:
        r = d["truth"]["redis"].get(p["tripId"])
        if not r or not r["intervals"]:
            continue
        design = [x / 1000 for x in reversed(r["intervals"])][-WINDOW:]
        arr = sorted(p["acked"].values())
        naive = [(b - a) / 1000 for a, b in zip(arr, arr[1:])][-WINDOW:]
        rows.append((design, naive))
    if not rows:
        return
    design, naive = rows[len(rows) // 2]

    def phi_curve(xs, ts):
        mu, sd = st.mean(xs), max(st.pstdev(xs), MIN_STD_S)
        nd = st.NormalDist(mu, sd)
        out = []
        for t in ts:
            tail = max(1 - nd.cdf(t), 1e-300)
            out.append(min(-__import__("math").log10(tail), 40))
        return out

    ts = [i * 0.5 for i in range(0, 241)]
    fig, ax = plt.subplots(figsize=(3.5, 2.1))
    ax.plot(ts, phi_curve(design, ts), color=DESIGN, linewidth=1.8)
    ax.plot(ts, phi_curve(naive, ts), color=BASELINE, linestyle="--", linewidth=1.8)
    ax.axhline(PHI_THRESHOLD, color=INK2, linewidth=0.8)
    ax.text(ts[-1], PHI_THRESHOLD + 0.6, f"suspect at φ = {PHI_THRESHOLD:g}", ha="right", va="bottom", color=INK2, fontsize=6.5)
    ax.text(14, 30, "Only single live check-ins\ncount (CareRoute)", color=INK, fontsize=6.5, va="center")
    ax.text(70, 4.0, "Every arrival counts,\nbatches included", color=INK, fontsize=6.5, va="center")
    ax.set_ylim(0, 40)
    ax.set_xlim(0, ts[-1])
    ax.set_xlabel("Seconds since the phone's last check-in")
    ax.set_ylabel("Suspicion φ")
    save(fig, "e4-heartbeat")


def main():
    summary = json.loads((RESULTS / "summary.json").read_text())
    fig_e1_loss(summary)
    fig_e1_budget()
    fig_e2(summary)
    fig_e3(summary)
    fig_e4()


if __name__ == "__main__":
    main()
