# Problem definitions

Which detector reports which problem type. A detector's `normal` vote is evidence only about the types in its row.

| Detector (`detectorId`) | Service | Problem types it reports | What it checks |
|---|---|---|---|
| `route-corridor` | `detector-route` | `off_route` | Last 3 check-ins all further than 100 m + GPS accuracy from the planned path |
| `heading-speed` | `detector-heading` | `wrong_direction` | Median direction of travel is over 120° from the path direction |
| `dwell-time` | `detector-dwell` | `long_dwell` | Stationary longer than 5 min walking / 20 min waiting for a bus or train |
| `route-progress` | `detector-progress` | `off_route`, `wrong_direction`, `missed_stop` | Distance left to the end measured along the route: off the corridor and not getting closer, going back along the route, or riding over 150 m past the step's end stop |

Notes for the Aggregator:

- `route-progress` votes `normal` with confidence **0.3** when the traveller is off the corridor but still getting closer to the end (a detour), so it cannot outvote `route-corridor` on `off_route`.
- `no_response`, `help_requested` and `disruption` don't come from detectors (Heartbeat Monitor, traveller app and Disruption Lambda).
