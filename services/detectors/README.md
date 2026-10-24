# Detectors (M4)

One codebase and one image. `DETECTOR_ID` selects which detector a container runs:
`route-corridor`, `heading-speed`, `dwell-time` (and `ml-anomaly` later).

The Coordinator (M3) calls `POST /internal/detect` with `contracts/examples/detector-request.json`
and gets back `contracts/examples/detector-vote.json`. Verdicts are `normal`, `problem` or `abstain`.
`abstain` means "can't decide" and always has `problemType: null` and `confidence: 0`.

## Layout

| Path | What |
|---|---|
| `detectors/models.py` | Pydantic models matching the contract JSON (camelCase on the wire) |
| `detectors/config.py` | Settings and thresholds from env vars (`Settings.from_env()`) |
| `tests/` | pytest; `test_models.py` checks every shared contract example still parses |

## Run the tests

```bash
cd services/detectors
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Settings

Each env var is the field name in upper case. Defaults are in `detectors/config.py`.

| Env var | Default | Used by |
|---|---|---|
| `DETECTOR_ID` | none | all |
| `REDIS_URL` | `redis://localhost:6379/0` | all |
| `GPS_MAX_ACCURACY_M` | 50 | all (abstain above this) |
| `CORRIDOR_M`, `CORRIDOR_CONSECUTIVE` | 100, 3 | route-corridor |
| `MIN_MOVING_SPEED_MPS`, `WRONG_DIR_DEG`, `WALK_MAX_SPEED_MPS` | 0.5, 120, 4 | heading-speed |
| `DWELL_RADIUS_M`, `DWELL_WALK_S`, `DWELL_TRANSIT_S` | 30, 300, 1200 | dwell-time |
