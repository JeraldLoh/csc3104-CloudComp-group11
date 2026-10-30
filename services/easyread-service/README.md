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
- `app/hallucination_check.py` checks each AI step. If the AI dropped a bus number, stop name or stop count, added a
  number that isn't in the step, or wrote more than 300 characters, the step gets `text` back with `fallback: true`.
- If Ollama is down, slow or answers with something that isn't JSON, every step gets `text` and `provider: "template"`.
  The endpoint always answers `200` with usable wording.

## Files

| File | |
|---|---|
| `app/main.py` | the endpoints |
| `app/ollama_client.py` | the prompt and the one `/api/chat` call per route (JSON schema answer, temperature 0) |
| `app/hallucination_check.py` | the checks above |
| `app/config.py` | `OLLAMA_URL` (`http://ollama:11434`), `OLLAMA_MODEL` (`qwen2.5:1.5b`), `OLLAMA_TIMEOUT_S` (60), `OLLAMA_KEEP_ALIVE` (`30m`), `EASYREAD_MAX_CHARS` (300) |

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
