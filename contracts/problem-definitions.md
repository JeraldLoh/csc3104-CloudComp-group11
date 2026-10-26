# Problem definitions

**Status: DRAFT for review.** M4 (detectors, simulator) owns the detection rules; M5 (Alert Service)
and M3 (Aggregator, experiment) review. Comment on the pull request.

The other contract files say **what** the problem types are called. This file says **which detector**
reports each one and **when** something counts as it, so the detectors, the simulator's ground-truth
labels, the alerts and the experiment all mean the same thing.

## Which detector reports which problem type

A detector's `normal` vote is evidence only about the types in its row.

| Detector (`detectorId`) | Service | Problem types it reports | What it checks |
|---|---|---|---|
| `route-corridor` | `detector-route` | `off_route` | Last 3 check-ins all further than 100 m + GPS accuracy from the planned path |
| `heading-speed` | `detector-heading` | `wrong_direction` | Median direction of travel is over 120° from the path direction |
| `dwell-time` | `detector-dwell` | `long_dwell` | Stationary longer than 5 min walking / 20 min waiting for a bus or train |
| `route-progress` | `detector-progress` | `off_route`, `wrong_direction`, `missed_stop` | Distance left to the end measured along the route: off the corridor and not getting closer, going back along the route, or riding over 150 m past the step's end stop |

So `off_route` and `wrong_direction` each have two detectors that judge them; `long_dwell` and
`missed_stop` have one. There is no `ml-anomaly` detector (not planned).

## Thresholds

The numbers are what the detectors do today (`services/detectors/detectors/config.py`). Each can be
changed through an environment variable without a rebuild; the simulator will be used to tune them.

| Problem type | Definition | Thresholds (env var, default) |
|---|---|---|
| `off_route` | Further from the planned path than allowed, for several check-ins in a row. The allowance grows with GPS accuracy, so a fuzzy fix can't trigger it. route-progress also requires not getting closer to the end. | `CORRIDOR_M` 100 m + the check-in's `accuracyM`; `CORRIDOR_CONSECUTIVE` 3 check-ins (about 30 s); progress smaller than `PROGRESS_MIN_M` 20 m + GPS accuracy doesn't count as progress |
| `wrong_direction` | Moving against the route: the median direction differs from the route's direction too much (heading-speed), or the traveller is going back along the route (route-progress). | `WRONG_DIR_DEG` 120°; at least 3 moving check-ins; moving means at least `MIN_MOVING_SPEED_MPS` 0.5 m/s |
| `long_dwell` | Staying in one place longer than expected for the current step. | Within `DWELL_RADIUS_M` 30 m for more than `DWELL_WALK_S` 300 s (walking step) or `DWELL_TRANSIT_S` 1200 s (bus/train step) |
| `missed_stop` | On a bus/train step, carried past the stop where the traveller should get off. | Past the step's end stop along the route and more than `MISSED_STOP_M` 150 m (+ GPS accuracy) from it, for the last 3 check-ins |

### When a detector abstains ("can't decide")

Abstaining is never counted as "normal". Every detector abstains when:
- the trip's route isn't in Redis, or the check-in's `routeVersion` doesn't match the trip's;
- the check-in has no GPS accuracy, or accuracy is worse than `GPS_MAX_ACCURACY_M` (50 m);
- there's too little usable history (route-corridor: fewer than 3 usable check-ins; heading-speed:
  fewer than 3 moving check-ins, since standing still says nothing about direction).

dwell-time also abstains when the whole history window is stationary and already a quarter of the way
to the limit: it can't tell how much longer the traveller has been there (open question 1).

## Reported directly (no detector)

| Problem type | Definition | Raised by |
|---|---|---|
| `no_response` | The traveller's check-ins have gone silent for longer than their usual rhythm allows (φ accrual failure detector). | Heartbeat Monitor (M1) |
| `help_requested` | The traveller pressed the help button. Always an alert. | Traveller app → Alert Service `POST /api/help` (M2, M5) |
| `disruption` | A transport disruption (LTA DataMall) affects a step of the trip. | Disruption Lambda (M6) → `careroute-disruptions` |

## From votes to a decision (Aggregator, M3)

- Each problem type is judged only by the detectors that report it (the first table). A "normal" from
  dwell-time is not a vote against `off_route`.
- Weight of a vote = confidence × the detector's reliability (`detector:weights`, default 1.0).
  Abstains don't count.
- A problem is confirmed when its weighted share of its jury is **≥ 0.5**. A tie counts as a problem
  (a missed alert is the unsafe failure).
- If only one detector that judges that problem has answered, or it's a close call (0.4–0.6), and
  another detector that judges it hasn't been asked yet, the Coordinator asks more detectors first.
- `route-progress` votes `normal` with confidence **0.3** when the traveller is off the corridor but
  still getting closer to the end (a detour), so it cannot outvote `route-corridor` on `off_route`
  (0.8 vs 0.3 is still a problem). It confirms an off-route; it isn't meant to overrule one.
- A confirmed problem is sent to the Alert Service as `problem-report.json`. The Alert Service keeps one
  incident per `travellerId#tripId#problemType#stepIndex`.

## Ground truth for the simulator and the experiment

Every simulated check-in is labelled with what the correct decision is (`expected`). The labels must
use the definitions above. Otherwise accuracy and false-alarm rates can't be measured fairly.

Proposed labelling rules (open question 2):
- A check-in is labelled with a problem type from the moment the definition above is met (e.g. the
  3rd consecutive off-route check-in), until the traveller is back to normal.
- Check-ins before that point are `normal`, even if the traveller has started drifting.

## Open questions

1. **dwell-time can't prove a long dwell with 10 check-ins** (about 100 s of history, shorter than the
   5 / 20 min limits). Could the Check-in Service store when the current dwell started
   (e.g. `dwellStartAt` in `trip:{tripId}:context`)? (M2, M4)
2. **Labelling rule:** is the problem label applied from the moment the definition is met (proposed
   above), or from when the traveller first left the route? This changes the measured detection delay. (M4, M3)
3. **Scenario names differ.** `contracts/api.md` lists `normal`, `missed_stop`, `wrong_direction`,
   `long_dwell`, `silence`, `tunnel`, `out_of_order`; the detector test data uses `normal`, `off_route`,
   `wrong_direction`, `long_dwell`, `normal_bus_wait`, `poor_gps`. Agree on one list (and add
   `off_route` to the contract). (M4)
4. **`long_dwell` and `missed_stop` have one detector each**, so a faulty detector there can only be
   outvoted by lowering its reliability weight. Acceptable for now? (M4, M3)

Resolved: `missed_stop` is now detected by `route-progress`; `off_route` and `wrong_direction` have a
second judge (`route-progress`); `ml-anomaly` is not planned.
