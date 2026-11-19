# Alert Service results (A1 and A2)

Written by `report.py` from `results/`. How the experiments work: [README.md](README.md).

## A1: the same problem reported many times at once

Run `1119175123`: 300 problems, each reported 20 times at once, the copies spread over the replicas in turn. *conditional + outbox* is as deployed: every write that must happen once (creating an incident, claiming a reminder, claiming a push) is a DynamoDB conditional write, and each push is kept on its incident until sent, so another replica sends it if the one that should have died. *read-then-write*: those writes read the incident, decide, then write without a condition. *no outbox*: the design before the outbox, where a replica pushes straight away and a push it didn't send is lost. *killed*: one replica killed (no shutdown) and started again a few seconds later, taking turns; a report that got no answer is sent again to the next replica. Expected per problem: 1 incident, 1 "created" answer, 1 push, and a detection count equal to the reports accepted.

| Condition | kills | reports | failed | incidents | "created" answers (extra) | pushes (duplicate / missing) | pushes taken over | detections recorded (lost / extra) | request p50 / p95 | first push p95 / max |
|---|---|---|---|---|---|---|---|---|---|---|
| conditional + outbox, 1 replica | 0 | 6000 | 0 | 300 | 300 (0) | 300 (0 / 0) | 0 | 6000 (0 / 0) | 163 ms / 224 ms | 947 ms / 1276 ms |
| conditional + outbox, 2 replicas | 0 | 6000 | 0 | 300 | 300 (0) | 300 (0 / 0) | 0 | 6000 (0 / 0) | 157 ms / 216 ms | 525 ms / 946 ms |
| conditional + outbox, 4 replicas | 0 | 6000 | 0 | 300 | 300 (0) | 300 (0 / 0) | 0 | 6000 (0 / 0) | 150 ms / 202 ms | 440 ms / 544 ms |
| read-then-write, no outbox, 1 replica | 0 | 6000 | 0 | 300 | 2808 (2508) | 2808 (2508 / 0) | – | 3384 (2616 / 0) | 136 ms / 175 ms | 32569 ms / 33150 ms |
| read-then-write, no outbox, 4 replicas | 0 | 6000 | 0 | 300 | 2599 (2299) | 2599 (2299 / 0) | – | 3655 (2345 / 0) | 175 ms / 215 ms | 1212 ms / 3410 ms |
| read-then-write + outbox, 4 replicas | 0 | 6000 | 0 | 300 | 3043 (2743) | 300 (0 / 0) | 0 | 3158 (2842 / 0) | 174 ms / 221 ms | 1700 ms / 2150 ms |
| conditional, no outbox, 2 replicas, one killed every 5 s | 9 | 6000 | 0 | 300 | 298 (0) | 291 (0 / 9) | – | 6050 (0 / 50) | 143 ms / 278 ms | 342 ms / 2008 ms |
| conditional + outbox, 2 replicas, one killed every 5 s | 10 | 6000 | 0 | 300 | 299 (0) | 300 (0 / 0) | 9 | 6049 (0 / 49) | 153 ms / 277 ms | 505 ms / 17136 ms |

*Lost* detections are reports accepted but not counted: two writers read the same count, or a racing create reset it to 1. *Extra* detections are reports saved whose answer was lost to a crash, so they were sent again (at-least-once delivery), which only raises the count. A *missing* push is a problem whose incident exists but no push was ever sent. *Taken over*: pushes the replica that saved the alert didn't send (it was killed first), sent by another replica once their lease ran out (`PUSH_LEASE_S`, 15 s); they are why the slowest first push takes about 15–20 s.

## A2: reminders with several replicas checking

Run `1119180025`: 50 urgent alerts nobody acknowledges. Every replica looks for due reminders every 1 s; a reminder is due 8 s after the alert before it, up to 3 (deployed: every 15 s, 120 s, 3). *conditional + outbox* is as deployed: every write that must happen once (creating an incident, claiming a reminder, claiming a push) is a DynamoDB conditional write, and each push is kept on its incident until sent, so another replica sends it if the one that should have died. *read-then-write*: those writes read the incident, decide, then write without a condition. *no outbox*: the design before the outbox, where a replica pushes straight away and a push it didn't send is lost. Here the conditional write that matters is the reminder's claim on `escalationLevel`. The push lease is scaled like the reminders (1 s; deployed 15 s). Expected: exactly 3 reminders per alert. *Late* is each reminder's gap from the alert before it minus the wait, so up to one check interval by design (timestamps are kept to the second, so it can be slightly negative).

| Condition | kills | reminders sent / expected | extra (alerts) | missing (alerts) | pushes taken over | at the last level | late p50 / p95 / max |
|---|---|---|---|---|---|---|---|
| conditional + outbox, 1 replica | 0 | 150 / 150 | 0 (0) | 0 (0) | 0 | 50 / 50 | -0.2 s / +0.9 s / +0.9 s |
| conditional + outbox, 2 replicas | 0 | 150 / 150 | 0 (0) | 0 (0) | 0 | 50 / 50 | +0.2 s / +0.5 s / +1.2 s |
| conditional + outbox, 4 replicas | 0 | 150 / 150 | 0 (0) | 0 (0) | 0 | 50 / 50 | +0.1 s / +0.8 s / +1.2 s |
| read-then-write, no outbox, 4 replicas | 0 | 467 / 150 | 317 (49) | 0 (0) | – | 50 / 50 | +0.4 s / +1.1 s / +2.1 s |
| read-then-write + outbox, 4 replicas | 0 | 151 / 150 | 1 (1) | 0 (0) | 1 | 50 / 50 | +0.5 s / +1.0 s / +2.0 s |
| conditional + outbox, 2 replicas, one killed every 4 s | 6 | 150 / 150 | 0 (0) | 0 (0) | 0 | 50 / 50 | +0.0 s / +0.8 s / +0.9 s |
