# Scripts

Helpers for running the stack on your own machine. Run them from the repo root.

To work on AWS, see the [repo README](../README.md):

- **Create the environment:** "Deploy the infrastructure" and "Deploy the application".
- **Deploy a service:** merge to `main`, or run the Services workflow by hand (Actions > Services > Run workflow, with
  one service name or `all`). The manifest rules and placeholders are under "Service manifests".
- **Delete everything:** "Teardown". The environment costs about $0.30/hour while it exists, so tear it down at the
  end of each session.

## Local stack, no AWS

`local-sim` runs everything on a kind cluster on your machine instead, for free: the services, the check-in
analysis, the caregiver app and, optionally, a simulated trip through the whole chain.

| What you want | Mac or Linux | Windows (PowerShell) |
|---|---|---|
| First time, and after changing service code | `bash scripts/macos/local-sim.sh --setup` | `.\scripts\windows\local-sim.ps1 -Setup` |
| Start everything and send a trip | `bash scripts/macos/local-sim.sh --scenario off_route` | `.\scripts\windows\local-sim.ps1 -Scenario off_route` |

Needs Docker or Podman, kind, kubectl, Node and Python, plus `.env` with `LOCALSTACK_AUTH_TOKEN`. Details in the
repo README, "Run locally with kind".

On Windows, if scripts are blocked, run them as `powershell -ExecutionPolicy Bypass -File scripts\windows\<script>.ps1 ...`.

## Port-forwards (Windows)

`.\scripts\windows\port-forwards.ps1` forwards the Alert Service (8005), Journey Service (8006) and Check-in Service
(8001) from a kind cluster to the ports the apps' dev servers expect, and restarts each forward when its pods are
replaced. Stop it with `Ctrl+C`. You don't need it with a cluster made by `local-sim -Setup`, which serves these ports
itself.
