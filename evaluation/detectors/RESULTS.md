# Detector results (E8 and E9)

These continue the team's experiments: E1 to E4 in [`evaluation/`](../RESULTS.md) cover the app
and the Check-in Service, and C1 to C5 in [`evaluation/coordinator/`](../coordinator/RESULTS.md)
cover detection accuracy, faulty detectors and redundancy modes through the Coordinator. These two measure the
detector services themselves in Kubernetes: how fast they answer, whether they scale out, and whether they stay
available when pods or Redis really fail.

| | Question | Compares |
|---|---|---|
| **E8** | Are the detectors fast enough, and do they scale out? | real-time load vs 10× load, in the kind cluster |
| **E9** | Do the detectors stay available when pods or Redis really fail? | a pod crash, a whole detector crashing, and a Redis outage, in the kind cluster |

Both ran on the local kind cluster on one laptop (rootless Podman), with every service from `local-sim.sh`.
Run the scripts from the repo root, with `kubectl` pointed at that cluster and `pip install -r evaluation/requirements.txt`
for the figures.

The runs used detector code with three fixes that were later reverted (bus-stop dwell limit, missed-stop
route-progress check, heading-speed step matching). Those only change what the detectors decide, not how they
are deployed, so they don't affect availability. Without them a detector does slightly less work per call, so
E8's latency is if anything a little pessimistic.

## E8: latency and scaling in the cluster

| | Real time | 10× load |
|---|---|---|
| Load | 100 travellers checking in every 10 s, 10 requests/s per detector, 3 min | 50 travellers every 0.5 s, 100 requests/s per detector, 5 min |
| Requests, errors | 7,200, 0 | 120,000, 0 |
| p50 / p95 per detector | 6.1 to 8.2 ms / 7.3 to 10.0 ms | 5.7 to 8.1 ms / 7.5 to 11.3 ms |
| Answers over the 500 ms budget | 0 | 0 |
| Replicas | stayed at 2, CPU at most 37% of the request | 2 → 4 within 45 s, back to 2 about 5 min after the load stopped |

- **At real-time load for 100 travellers, a detector answers in about 10 ms at p95,** 50 times inside the
  Coordinator's 500 ms budget, with the minimum 2 replicas.
- **At 10 times that load every detector scaled out to its maximum of 4 within 45 s,** and p95 latency stayed
  around 10 ms. CPU stayed at about 155% of the pods' request at 4 replicas, so the cap of 4, not the load,
  set the replica count. A higher `maxReplicas` would use more pods; at this load they weren't needed for
  latency. The HPAs scaled back after their default 5 minute stabilisation window.
- 50 travellers every 0.5 s sends the same 400 requests/s as 100 travellers every second, with fewer threads.
  On a laptop, rootless Podman caps the kind node at 2,048 threads for all pods together, and the first
  attempt at 100 travellers hit that cap in the load generator after 100 s. It held 400 requests/s at
  p95 8.5 to 10.5 ms until then.

How it was run, on the local kind cluster on one laptop (`k8s/local/load-test`, with `--travellers` and
`--interval` changed per run). The load generator writes Redis like the Check-in Service and calls each detector
Service by name from inside the cluster, so requests spread over every replica. The HPAs were sampled every 15 s.

```bash
kubectl apply -k k8s/local/metrics-server
kubectl apply -k k8s/local/load-test      # then kubectl logs -n csc3104 job/detector-load -f
python evaluation/detectors/scaling.py evaluation/detectors/results/e8-stress-hpa.csv \
  evaluation/detectors/results/e8-stress.log --out evaluation/detectors/results/e8-scaling.png
```

Raw data: `results/e8-nominal*` and `results/e8-stress*`, the HPA samples (`t_s,hpa,cpu,replicas`) and the
load job's output.

On EKS each pod gets 0.25 vCPU from Fargate, not a share of a laptop. So the absolute latency will differ
there, but the scale-out behaviour is the same HPA.

## E9: availability under real faults

A probe inside the cluster calls each of the four detectors 10 times a second, through their Kubernetes
Services, like the Coordinator does. That's about the load of 100 travellers checking in in real time. It keeps a
normal trip in Redis so the detectors can give real votes. A call counts as **answered** if it returned within
the Coordinator's 500 ms budget, and as a **real vote** if it was `normal` or `problem` rather than an abstain.
Three faults were injected into the running cluster, and ready replicas were sampled every second.

| Fault | Calls to the affected detector afterwards | Back to full strength | Others |
|---|---|---|---|
| One route-corridor pod crashes (force-killed) | 300 of 300 answered in the next 30 s, 0 errors | replacement ready after 6.8 s | unaffected |
| Both dwell-time pods crash | 229 of 300 answered in budget in the next 30 s, 61 failed: about 6 s with no dwell-time at all | first replacement ready after 7.7 s | 100% answered |
| Redis stops for 30 s | no real votes from any detector for about 44 s | detectors ready 14.8 s after Redis restarts | (all affected) |

| Whole run, 300 s, 12,000 calls | Answered in budget | Real votes |
|---|---|---|
| Before the Redis outage, including both pod crashes | 98.97% | |
| route-corridor alone, through its pod crash | 100% | |
| dwell-time alone, through losing both pods | 95.89% | |
| The 60 s around the Redis outage | 35.0% | 28.5% |
| Whole run | 86.38% | 84.95% |

- **Two replicas hide a pod crash completely.** When one route-corridor pod was killed without warning, the
  Service sent every call to the other replica: no call failed. Kubernetes had a replacement ready in under 7 s.
- **Losing every replica of a detector costs about 6 s.** That's how long Kubernetes took to start and ready
  new dwell-time pods. The Coordinator turns those calls into abstains, so that detector's problems go unjudged
  for that time (Member 3's C2 measures what that does to the decisions). The other detectors weren't affected. Spreading replicas across nodes and zones, as
  the EKS manifests do, makes losing both at once much less likely than on this one-node laptop cluster.
- **No pod was restarted, even during the Redis outage.** The liveness check doesn't touch Redis, so
  Kubernetes didn't kill healthy pods for a fault that wasn't theirs. Only readiness, which pings Redis, failed.
- **A Redis outage stops every detector, because all trip state is there.** For the first 14 s the detectors
  still answered, with an abstain, but each answer took about 1 s (p95 1,010 ms), past the 500 ms budget, so the
  Coordinator would record a timeout. Then readiness checks failed (every 5 s, 3 failures) and Kubernetes took
  all detector pods out of their Services, so calls were refused. Redis was back 10 s after restarting, and the
  detectors were ready and voting 14.8 s after. No pod restarts were needed to recover.
- Redis runs as a single pod without persistence locally, so the outage also lost its data. The probe wrote its
  trip again each second. On AWS, Redis is ElastiCache.

Things the detectors could do better, from these results:
- **Fail fast when Redis is down.** The detectors' Redis client waits up to 1 s per connection attempt, so during an
  outage every answer misses the 500 ms budget. A timeout of about 0.2 s would let them answer `abstain` within
  budget instead, with the real reason, while keeping each Redis call far inside the budget normally.
- **Readiness could react faster.** Taking about 14 s to pull detectors out of service is the default probe timing
  (`periodSeconds: 5`, `failureThreshold: 3`). Lowering it shortens the slow-answer phase but makes a brief Redis
  blip more likely to pull every pod.

The probe's own Redis writes cause the brief dips at about 7, 67 and 245 s: it rewrites its trip every second,
and a detector that reads mid-rewrite sees too few check-ins and abstains. Every call was still answered in
budget. That's the 0.24% of abstains in the first 30 s.

How to run it, on the local kind cluster with the simulator image loaded
(`docker build -t cloud-simulator:local -f tools/simulator/Dockerfile tools`). It takes about 5½ minutes, and
stopping Redis loses its local data, so don't run it during a demo:

```bash
python evaluation/detectors/availability.py               # results/e9-availability.json and .png
python evaluation/detectors/availability.py --chart-only  # redraw the figure
```

Wait for the detectors to be back at 2 replicas before running it again. The probe's own load can make the HPAs
scale up, which changes the recovery numbers.
