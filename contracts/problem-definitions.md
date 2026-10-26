# Problem definitions

**Status: DRAFT for review.** M4 (detectors, simulator) owns the detection rules; M5 (Alert Service)
and M3 (Aggregator, experiment) review. Comment on the pull request.

The other contract files say **what** the problem types are called. This file says **when** something
counts as each one, so the detectors, the simulator's ground-truth labels, the alerts and the
experiment all mean the same thing.

The numbers below are what the detectors do today (`services/detectors/detectors/config.py`). Each
can be changed through an environment variable without a rebuild; the simulator will be used to tune
them.

## Detected from check-ins

| Problem type | Definition | Detector | Thresholds (env var, default) |
|---|---|---|---|
| `off_route` | Further from the planned path than allowed, for several check-ins in a row. The allowance grows with GPS accuracy, so a fuzzy fix can't trigger it. | route-corridor | `CORRIDOR_M` 100 m + the check-in's `accuracyM`; `CORRIDOR_CONSECUTIVE` 3 check-ins (about 30 s) |
| `wrong_direction` | Moving against the direction of the route: the median difference between the direction travelled and the route's direction is too large. | heading-speed | `WRONG_DIR_DEG` 120°; at least 3 moving check-ins; moving means at least `MIN_MOVING_SPEED_MPS` 0.5 m/s |
| `long_dwell` | Staying in one place longer than expected for the current step. | dwell-time | Within `DWELL_RADIUS_M` 30 m for more than `DWELL_WALK_S` 300 s (walking step) or `DWELL_TRANSIT_S` 1200 s (bus/MRT step) |
| `missed_stop` | On a bus/MRT step, carried past the stop where the traveller should get off. | **None yet** (open question 1) | — |

### When a detector abstains ("can't decide")

Abstaining is never counted as "normal". Every detector abstains when:
- the trip's route isn't in Redis, or the check-in's `routeVersion` doesn't match the trip's;
- the check-in has no GPS accuracy, or accuracy is worse than `GPS_MAX_ACCURACY_M` (50 m);
- there's too little usable history (route-corridor: fewer than 3 usable check-ins; heading-speed:
  fewer than 3 moving check-ins, since standing still says nothing about direction).

dwell-time also abstains when the whole history window is stationary and already a quarter of the way
to the limit: it can't tell how much longer the traveller has been there (open question 2).

## Reported directly (no detector)

| Problem type | Definition | Raised by |
|---|---|---|
| `no_response` | The traveller's check-ins have gone silent for longer than their usual rhythm allows (φ accrual failure detector). | Heartbeat Monitor (M1) |
| `help_requested` | The traveller pressed the help button. Always an alert. | Traveller app → Alert Service `POST /api/help` (M2, M5) |
| `disruption` | A transport disruption (LTA DataMall) affects a step of the trip. | Disruption Lambda (M6) → `careroute-disruptions` |

## From votes to a decision (Aggregator, M3)

- Each problem type is judged only by the detectors that check it (the table above), plus `ml-anomaly`,
  which checks everything. A "normal" from dwell-time is not a vote against `off_route`.
- Weight of a vote = confidence × the detector's reliability (`detector:weights`, default 1.0).
  Abstains don't count.
- A problem is confirmed when its weighted share of its jury is **≥ 0.5**. A tie counts as a problem
  (a missed alert is the unsafe failure).
- If only one detector that checks that problem has answered, or it's a close call (0.4–0.6), and
  another detector that checks it hasn't been asked yet, the Coordinator asks more detectors first.
- A confirmed problem is sent to the Alert Service as `problem-report.json`. The Alert Service keeps one
  incident per `travellerId#tripId#problemType#stepIndex`.

## Ground truth for the simulator and the experiment

Every simulated check-in is labelled with what the correct decision is (`expected`). The labels must
use the definitions above. Otherwise accuracy and false-alarm rates can't be measured fairly.

Proposed labelling rules (open question 3):
- A check-in is labelled with a problem type from the moment the definition above is met (e.g. the
  3rd consecutive off-route check-in), until the traveller is back to normal.
- Check-ins before that point are `normal`, even if the traveller has started drifting.

## Open questions

1. **`missed_stop` has no detector.** It's a contract problem type and a simulator scenario. Extend
   route-corridor (progress along the path past the step's alighting stop, still at vehicle speed), or
   add a detector? (M4)
2. **dwell-time can't prove a long dwell with 10 check-ins** (about 100 s of history, shorter than the
   5 / 20 min limits). Could the Check-in Service store when the current dwell started
   (e.g. `dwellStartAt` in `trip:{tripId}:context`)? (M2, M4)
3. **Labelling rule:** is the problem label applied from the moment the definition is met (proposed
   above), or from when the traveller first left the route? This changes the measured detection delay. (M4, M3)
4. **Scenario names differ.** `contracts/api.md` lists `normal`, `missed_stop`, `wrong_direction`,
   `long_dwell`, `silence`, `tunnel`, `out_of_order`; the detector test data uses `normal`, `off_route`,
   `wrong_direction`, `long_dwell`, `normal_bus_wait`, `poor_gps`. Agree on one list (and add
   `off_route` to the contract). (M4)
5. **One detector per problem type.** Until `ml-anomaly` exists, each problem has only one detector, so a
   faulty detector can only be outvoted by lowering its reliability weight. Is that acceptable, or should
   another detector also judge `off_route`? (M4, M3)
