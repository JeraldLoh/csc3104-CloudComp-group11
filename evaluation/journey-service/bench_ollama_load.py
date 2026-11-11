"""How many "Simplify with AI" requests at once can Ollama take before caregivers stop getting AI wording?

    kubectl port-forward svc/easyread-service 8007:8000 -n csc3104      (kind; Docker Compose already uses 8007)
    python services/journey-service/benchmarks/bench_ollama_load.py
    python services/journey-service/benchmarks/bench_ollama_load.py --levels 1,2,4,8,16 --keep-going

It sends route steps straight to the Easy-read Service (POST /internal/easyread), which is what the Journey Service
does, so nothing else (OneMap, Street View, S3) is in the measurement.

What "failing" means. The Easy-read Service never answers with an error when Ollama can't keep up: after
OLLAMA_TIMEOUT_S (75 s) it gives the template wording back with `provider: "template"`. So a request has failed when
it got no AI wording: provider is not "ollama", an HTTP error, or no answer within --timeout-s (90 s, as long as the
Journey Service waits). Steps the AI wrote but the hallucination check refused are counted separately (`fallback`):
that is about the wording, not about load.

It first times one request alone (T1). Unless --levels says otherwise, the levels tested are picked around
OLLAMA_TIMEOUT_S / T1, which is where a queue served one request at a time would run out of time, so the limit is
found in a few bursts. It stops at the first level where a request fails (--keep-going tests them all).
"""
import argparse
import sys
import threading
import time

from _common import call, finish, stop, summary, table

# A route's steps as the Journey Service sends them: its template wording, and the facts that must survive (`keep`).
# {bus} and {stop} differ per request, so no two prompts are the same.
STEPS = [
    {"mode": "walk", "text": "Walk 150 m to bus stop {stop} at Opposite Fengshan Primary School.",
     "keep": ["{stop}", "Opposite Fengshan Primary School"]},
    {"mode": "bus", "text": "Find bus stop {stop} at Opposite Fengshan Primary School and check the number on the "
                            "pole, then raise your hand for bus {bus} to Tampines Interchange, which comes every 8 to "
                            "12 minutes. Tap your card to get on. After Block 731, press the bell and get off at "
                            "Tanah Merah Station Exit B. Tap your card to get off.",
     "keep": ["{stop}", "{bus}", "Opposite Fengshan Primary School", "Tampines Interchange", "Block 731",
              "Tanah Merah Station Exit B"]},
    {"mode": "walk", "text": "Walk 80 m to Tanah Merah MRT Station.", "keep": ["Tanah Merah MRT Station"]},
    {"mode": "train", "text": "Tap your card at the gantry. Follow the green signs for the East West Line. Take the "
                              "train going to Tuas Link. Get off at Paya Lebar MRT Station. It is the 4th stop.",
     "keep": ["East West Line", "Tuas Link", "Paya Lebar MRT Station", "4th"]},
    {"mode": "walk", "text": "Tap your card at the gantry to go out. Walk 200 m to [place 1]. You have arrived.",
     "keep": ["[place 1]"]},
    {"mode": "bus", "text": "Find bus stop 81111 at Paya Lebar Station Exit C and check the number on the pole, then "
                            "raise your hand for bus {bus} to Eunos Interchange. Tap your card to get on. After "
                            "Block 12, press the bell and get off at Opposite Haig Road Market. Tap your card to "
                            "get off.",
     "keep": ["81111", "{bus}", "Paya Lebar Station Exit C", "Eunos Interchange", "Block 12",
              "Opposite Haig Road Market"]},
]


def body(n: int, steps: int) -> dict:
    """Request number `n`'s route: `steps` steps, with its own bus number and stop code."""
    fill = {"bus": str(10 + n % 180), "stop": str(85001 + n % 900)}
    return {"steps": [{"stepIndex": i, "mode": s["mode"], "text": s["text"].format(**fill),
                       "keep": [k.format(**fill) for k in s["keep"]]}
                      for i, s in enumerate(STEPS[j % len(STEPS)] for j in range(steps))]}


def ask(url: str, n: int, steps: int, timeout: float) -> dict:
    r = call("POST", f"{url}/internal/easyread", body(n, steps), timeout=timeout)
    answer = r.json() if r.status == 200 else {}
    if r.status != 200:
        failure = r.error or f"HTTP {r.status}"
    elif answer.get("provider") != "ollama":
        failure = "template wording (Ollama timed out or is down)"
    else:
        failure = None
    return {"seconds": r.seconds, "failure": failure,
            "fallbackSteps": sum(bool(s.get("fallback")) for s in answer.get("steps") or [])}


def burst(url: str, concurrency: int, steps: int, timeout: float, first: int) -> list[dict]:
    """`concurrency` requests sent at the same moment (a barrier lets every thread go together)."""
    results: list[dict | None] = [None] * concurrency
    gate = threading.Barrier(concurrency)

    def one(i: int) -> None:
        gate.wait()
        results[i] = ask(url, first + i, steps, timeout)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def levels_around(estimate: int, most: int) -> list[int]:
    """Concurrency levels that close in on the estimated limit from both sides."""
    wanted = {1, 2, estimate // 2, estimate - 1, estimate, estimate + 1, round(estimate * 1.5)}
    return sorted(c for c in wanted if 1 <= c <= most)


def drain(url: str, steps: int, timeout: float, t1: float) -> None:
    """After a level that failed, Ollama may still be working through its queue: wait until one request is quick again."""
    for _ in range(12):
        r = ask(url, 0, steps, timeout)
        if not r["failure"] and r["seconds"] <= 2 * t1:
            return
        time.sleep(5)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--easyread-url", default="http://localhost:8007")
    ap.add_argument("--levels", help="concurrency levels to test, e.g. 1,2,4,8,16 (default: chosen around OLLAMA_TIMEOUT_S / T1)")
    ap.add_argument("--max-concurrency", type=int, default=48, help="highest level chosen automatically (default 48)")
    ap.add_argument("--steps", type=int, default=5, help="steps in each request's route (default 5)")
    ap.add_argument("--baseline-runs", type=int, default=3, help="single requests timed for T1 (default 3)")
    ap.add_argument("--ollama-timeout-s", type=float, default=75,
                    help="the Easy-read Service's OLLAMA_TIMEOUT_S, used to choose the levels (default 75)")
    ap.add_argument("--timeout-s", type=float, default=90, help="how long a request may take (default 90, EASYREAD_TIMEOUT_S)")
    ap.add_argument("--keep-going", action="store_true", help="test every level, even after one has failed")
    args = ap.parse_args()
    url = args.easyread_url.rstrip("/")

    print("\n=== Easy-read Service + Ollama: concurrent requests before failure ===\n")

    ready = call("GET", f"{url}/ready", timeout=5)
    if ready.status != 200:
        stop(f"the Easy-read Service at {url} is not ready ({ready.error or ready.status}). On kind, first run: "
             f"kubectl port-forward svc/easyread-service 8007:8000 -n csc3104")

    # T1: one request alone, after one that loads the model into memory.
    warm_up = ask(url, 0, args.steps, 180)
    if warm_up["failure"]:
        stop(f"a single request already fails: {warm_up['failure']}")
    alone = [ask(url, 1 + i, args.steps, args.timeout_s) for i in range(args.baseline_runs)]
    if any(r["failure"] for r in alone):
        stop(f"a single request fails: {next(r['failure'] for r in alone if r['failure'])}")
    t1 = summary([r["seconds"] for r in alone])["p50"]
    estimate = int(args.ollama_timeout_s // t1) + 1
    levels = ([int(x) for x in args.levels.split(",")] if args.levels
              else levels_around(estimate, args.max_concurrency))
    print(f"Model load + first request: {warm_up['seconds']:.1f} s. One {args.steps}-step request alone: "
          f"T1 = {t1:.1f} s (median of {len(alone)}).")
    print(f"Testing levels {levels} (requests at once).\n")

    rows, tested, sent = [], [], 100
    for c in levels:
        started = time.perf_counter()
        results = burst(url, c, args.steps, args.timeout_s, sent)
        wall = time.perf_counter() - started
        sent += c
        ok = [r for r in results if not r["failure"]]
        failed = [r for r in results if r["failure"]]
        s = summary([r["seconds"] for r in ok])
        level = {"concurrency": c, "ok": len(ok), "failed": len(failed), "wallS": wall,
                 "latencyS": s, "throughputPerMin": len(ok) / wall * 60,
                 "ratioToCT1": s["max"] / (c * t1) if ok else None,
                 "failures": sorted({r["failure"] for r in failed}),
                 "fallbackSteps": sum(r["fallbackSteps"] for r in ok), "requests": results}
        tested.append(level)
        rows.append([c, f"{len(ok)}/{c}", f"{s['p50']:.1f}" if ok else "-", f"{s['p95']:.1f}" if ok else "-",
                     f"{s['max']:.1f}" if ok else "-", f"{level['ratioToCT1']:.2f}" if ok else "-",
                     f"{level['throughputPerMin']:.1f}", f"{level['fallbackSteps']}/{len(ok) * args.steps}",
                     "; ".join(level["failures"]) or "-"])
        print(f"  {c:>3} at once: {len(ok)}/{c} got AI wording in {wall:.0f} s"
              + (f" (failures: {'; '.join(level['failures'])})" if failed else ""))
        if failed:
            if not args.keep_going:
                break
            drain(url, args.steps, args.timeout_s, t1)

    print("\n" + table(["at once", "AI wording", "p50 s", "p95 s", "max s", "max / (c x T1)", "ok per min",
                        "fallback steps", "failures"], rows))

    first_fail = next((lv["concurrency"] for lv in tested if lv["failed"]), None)
    last_ok = max((lv["concurrency"] for lv in tested if not lv["failed"]
                   and (first_fail is None or lv["concurrency"] < first_fail)), default=0)
    print(f"\nCapacity: {last_ok} requests at once all got AI wording"
          + (f"; at {first_fail} they start to fail." if first_fail else " (no failing level found)."))

    finish("ollama-load", {"settings": vars(args), "t1S": t1, "warmUpS": warm_up["seconds"],
                           "lastAllOk": last_ok, "firstFailure": first_fail, "levels": tested})
    return 0


if __name__ == "__main__":
    sys.exit(main())
