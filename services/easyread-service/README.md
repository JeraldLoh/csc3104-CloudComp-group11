# Easy-read Service (Member 6)

Rewrites route steps in Easy Read English (short sentences, one action each) for travellers with intellectual
disabilities, using a small language model on [Ollama](https://github.com/ollama/ollama). Only the Journey Service
calls it, when a caregiver presses **Simplify with AI** on a saved route; the caregiver checks the wording before it
is saved.

| Method | Path | |
|---|---|---|
| POST | `/internal/easyread` | steps -> simpler wording (contract: `contracts/api.md`, "Easy-read Service") |
| GET | `/health` | the process is up |
| GET | `/ready` | Ollama answers and the model is pulled |

## How it stays safe

- It never sees names or addresses: the Journey Service sends `[place N]` tokens instead.
- Every step arrives with its template wording (`text`) and the facts the wording must keep (`keep`).
- `app/hallucination_check.py` checks each AI step. If the AI dropped a bus number, stop name, stop count or landmark,
  added a number or a name that isn't in the step, or wrote more than the limit (`maxChars` in the request: journey-service's `EASYREAD_MAX_CHARS`, 300; this
  service's own `EASYREAD_MAX_CHARS` only when a request doesn't say), the step gets `text` back with
  `fallback: true`.
- If Ollama is down, slow or answers with something that isn't JSON, every step gets `text` and `provider: "template"`.
  The endpoint always answers `200` with usable wording.

## Landmarks

Steps may carry `spots`: the buildings the traveller will see along them, found by the Journey Service (OneMap), with
their kind, distance and side, never coordinates. The model gets four small jobs, because one big one (choose, place
and reword at once) made a 1.5B model mix up steps and make landmarks up:

1. **Look up:** it gets a `nearby` tool and asks which buildings are at each step's spots (up to `OLLAMA_TOOL_ROUNDS`
   rounds). Answers come only from the step's own `spots`. A spot it didn't ask about is still offered in job 2.
2. **Choose:** one answer per spot, the building the traveller can't miss. The JSON schema allows only that spot's own
   names ("none" only where all are plain buildings), so it can't make one up or take one from another step.
3. **Place:** code adds a sentence for each choice where it is seen and adds the names to `keep`. A bus landmark
   joins the journey service's bell cue ("After Block 301, look out for City Plaza on your left. Press the bell.");
   a walk's goes before "You have arrived.". To stay within 300 characters it leaves out, in order: the ride's
   length, the landmark at the stop, the stop's name in the bell cue (the landmark alone is the cue), how often the
   bus comes, then the other landmarks.
4. **Reword:** the usual rewrite, then the checks above. A step that fails gets the template text *with* the landmark
   sentences, so its landmarks are kept either way. The ids of the ones used come back in `landmarks`.

`EASYREAD_LANDMARKS=given` skips job 1 (one call fewer, the same choices); `off` ignores `spots`. On a laptop CPU a
three-step route with landmarks takes about 6 s with `qwen2.5:1.5b` (four calls). `qwen2.5:3b` was no better at the
tool calls.

## Files

| File | |
|---|---|
| `app/main.py` | the endpoints |
| `app/ollama_client.py` | the prompts and the `/api/chat` calls (one per route, four with landmarks; JSON schema answers, temperature 0, fixed seed) |
| `app/hallucination_check.py` | the checks above |
| `app/config.py` | `OLLAMA_URL` (`http://ollama:11434`), `OLLAMA_MODEL` (`qwen2.5:1.5b`), `OLLAMA_TIMEOUT_S` (75, all calls for one route), `OLLAMA_KEEP_ALIVE` (`30m`), `EASYREAD_MAX_CHARS` (300, when a request has no `maxChars`), `EASYREAD_LANDMARKS` (`tools`), `OLLAMA_TOOL_ROUNDS` (4) |

The model is small because the cluster has no GPU: about 1 GB, a few seconds per route on 2 vCPUs. A larger model
(`OLLAMA_MODEL=llama3.2:3b`) writes better text locally if your machine has the memory.

## Running it

**Local (with the Journey Service):** `compose.journey.yaml` in `services/journey-service` starts this service
(port 8007) and Ollama. Ollama downloads the model into the `ollama-models` volume the first time:

```
docker compose --env-file .env -f services/journey-service/compose.journey.yaml up --build -d
docker compose -f services/journey-service/compose.journey.yaml logs -f ollama     # wait for the pull to finish
curl localhost:8007/ready
curl -X POST localhost:8007/internal/easyread -H "Content-Type: application/json" -d '{"steps": [{"stepIndex": 0,
  "mode": "bus", "text": "At Punggol Interchange, wait for bus number 190. Get on and tap your card. Get off at
  Opposite Block 221 after 6 stops.", "keep": ["190", "Punggol Interchange", "Opposite Block 221", "6"]}]}'
```

**kind:** `k8s/local/easyread-service.yaml` (part of `k8s/local`):

```
docker build -t easyread-service:local services/easyread-service
kind load docker-image easyread-service:local --name csc3104-local
```

**EKS:** `.\scripts\windows\deploy.ps1 -Service easyread-service` (or `bash scripts/macos/deploy.sh --service
easyread-service`) builds and deploys this service and Ollama from `k8s/eks/services/easyread-service.yaml`. Ollama
asks for 2 vCPU / 4 GB (about USD 0.12/hour on Fargate) and downloads the model each time its pod starts. Then set
`EASYREAD_URL=http://easyread-service:8000` on the Journey Service (already in its manifests).

**Tests:** `pip install -r requirements-dev.txt`, then `pytest` (Ollama is mocked).
