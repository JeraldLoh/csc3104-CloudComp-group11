# Detector results (E8 and E9)

E8 and E9 measure the detector services in Kubernetes: how fast they answer, whether they scale out, and whether
they stay available when pods or Redis fail. Detection accuracy and faulty detectors are covered by the
Coordinator's C1 to C5 ([`evaluation/coordinator/`](../coordinator/RESULTS.md)); the app and Check-in Service by
E1 to E4 ([`evaluation/`](../RESULTS.md)).

Both ran on the local kind cluster on one laptop (rootless Podman). Run the scripts from the repo root with
`kubectl` on that cluster, and `pip install -r evaluation/requirements.txt` for the figures (written to `results/`).
E8 and E9's before run had the whole local stack and detectors with three since-reverted fixes; E9's after run had
only Redis and the four detectors. Neither changes how the detector services answer, scale or recover.

## E8: latency and scaling

A load job inside the cluster writes Redis like the Check-in Service and calls each detector's Service, so requests
spread over every replica.

| | Real time | 10× load |
|---|---|---|
| Load per detector | 10 requests/s for 3 min (100 travellers every 10 s) | 100 requests/s for 5 min |
| Requests, errors, over the 500 ms budget | 7,200, 0, 0 | 120,000, 0, 0 |
| p95 per detector | 7.3 to 10.0 ms | 7.5 to 11.3 ms |
| Replicas | stayed at 2 (CPU at most 37%) | 2 → 4 within 45 s, back to 2 about 5 min after |

- At real-time load a detector answers in about 10 ms at p95, 50 times inside the Coordinator's budget.
- At 10× load the HPA scaled every detector out to its maximum of 4 within 45 s and p95 stayed around 10 ms, so
  the system stays effective as load grows. CPU was still 155% of the request at 4 replicas: the cap, not the load,
  set the replica count.
- The 10× run used 50 travellers every 0.5 s rather than 100 every second (the same 400 requests/s), because
  rootless Podman caps the kind node at 2,048 threads.

```bash
kubectl apply -k k8s/local/metrics-server
kubectl apply -k k8s/local/load-test      # then kubectl logs -n csc3104 job/detector-load -f
python evaluation/detectors/scaling.py evaluation/detectors/results/e8-stress-hpa.csv \
  evaluation/detectors/results/e8-stress.log --out evaluation/detectors/results/e8-scaling.png
```

## E9: availability under real faults

A probe inside the cluster calls each detector's Service 10 times a second, as the Coordinator would, while three
faults are injected. A call counts as answered if it returns within the 500 ms budget. E9 ran before and after the
fix it found (`results/e9-availability-before.*` and `results/e9-availability.*`).

| Fault | What happened (after the fix) | What it shows |
|---|---|---|
| One route-corridor pod killed | 300 of 300 calls answered, replacement ready in 5.9 s | The crash is masked by redundancy (Service plus 2 replicas); the reconcile loop recovers the replica |
| Both dwell-time pods killed | About 5 s with no dwell-time answers (54 of 300 calls), replacement ready in 6.9 s | With no replica left the crash isn't masked: the Coordinator sees an omission and tolerates it as an abstain |
| Redis stopped for 30 s | Abstains within budget until readiness pulled the pods after 11.9 s; ready again 11.9 s after Redis returned; 0 pod restarts | See below |

| Redis outage, while detectors were still in service | Before (1 s Redis timeout) | After (0.2 s) |
|---|---|---|
| Calls answered within 500 ms | 21.8% | 100% |
| p95 latency | 1,012 ms | 204 ms |

- **The fix turns a timing failure into an omission.** All trip state is in Redis, so a Redis outage stops every
  detector. Before, each answer came after the Redis client's 1 s timeout, past the deadline, so the Coordinator
  recorded a timeout. With `REDIS_TIMEOUT_S=0.2` every call got an `abstain` in budget, with the reason
  `internal error: Redis unavailable (TimeoutError)`.
- **Readiness is an unreliable heartbeat failure detector.** Kubernetes checks `/ready` every 5 s
  (`periodSeconds`) and pulls a pod after 3 misses (`failureThreshold`), so 10 to 15 s after Redis goes. It can only
  suspect: the pods were healthy, and they returned once Redis did. After that, calls are refused, so over the
  whole 30 s outage only 41% were answered (12% before).
- **Liveness doesn't touch Redis,** so no healthy pod was restarted for a fault that wasn't its own (no false
  positives).
- Before the Redis outage, including both pod crashes, 99.0% of calls were answered in budget in both runs.
- 35 to 55 s after both dwell-time pods were killed (about 30 to 50 s after their replacements were ready), 7 of 40
  calls to dwell-time failed in four separate seconds while both pods counted as ready. The probe doesn't record the
  kind of failure and the pods' logs are gone, so the cause is unknown. One possibility, not verified, is calls still
  routed to the force-deleted pods' old addresses. Recording the failure kind and the Service's endpoints in the
  probe would show the cause.

**Readiness timing trade-off.** Each readiness setting was run against a 3 s Redis pause (`CLIENT PAUSE`, which
should not pull pods) and then a 30 s outage (which should):

| Readiness probe | 3 s pause | Outage: first / all pods out of service | Outage calls answered in budget |
|---|---|---|---|
| Default (every 5 s, 3 misses) | no pod pulled | 10.9 s / 15.9 s | 39.8% |
| Fast (every 2 s, 2 misses) | every detector pulled for about 2 s | 3.0 s / 4.0 s | 11.3% |

Faster probes detect the outage four times sooner but suspect healthy pods during a brief pause: fewer false
negatives for more false positives. With the 0.2 s Redis timeout, detectors already answer `abstain` in budget while
Redis is down, so pulling them sooner only turns those answers into refused calls (11% answered instead of 40%).
The default setting is the better choice here.

```bash
python evaluation/detectors/availability.py               # about 5.5 min, stops Redis (loses local data)
python evaluation/detectors/availability.py --chart-only  # redraw the figure
python evaluation/detectors/availability.py --readiness   # about 8 min: both readiness settings, then restored
```

Wait for the detectors to be back at 2 replicas before running it again.
