"""Talks to Ollama (https://github.com/ollama/ollama/blob/main/docs/api.md).

    OllamaClient.rewrite(steps) -> {stepIndex: AI text}   one /api/chat call for the whole route
    OllamaClient.ready()        -> bool                   Ollama answers and has the model pulled

The answer is forced into JSON by `format` (a JSON schema) and kept repeatable with temperature 0.
Anything the AI writes still goes through hallucination_check.py before it is used.
"""
import json

import httpx

SYSTEM_PROMPT = """You rewrite public transport instructions in Easy Read English for adults with \
intellectual disabilities who travel alone in Singapore.

The app shows every sentence as its own bullet point, so write a short checklist.

Rules:
- One action per sentence. Never join two actions with "and" or a comma.
- Start every sentence with a verb, for example Go, Walk, Wait, Get on, Tap, Count, Take, Get off.
- At most 6 words per sentence, not counting stop and station names.
- Speak to the reader as "you". Use present tense. Use common, short words.
- No idioms, no abbreviations, no brackets apart from the [place N] tokens.
- Copy every bus number, stop name, station name, number and [place N] token exactly as written.
- Never add a stop, bus, exit, number, direction or landmark that is not in the input.
- Keep the steps in the same order and keep each stepIndex.

Example input step: {"stepIndex": 7, "mode": "bus", "text": "At Opposite Fengshan Primary School, wait \
for bus number 45 and get on, then tap your card. Get off at Tanah Merah Station Exit 2 after 5 stops."}
Example output step: {"stepIndex": 7, "easyRead": "Go to Opposite Fengshan Primary School. \
Wait for bus number 45. Get on the bus. Tap your card. Count 5 stops. Get off at Tanah Merah Station Exit 2."}

Answer with JSON only: {"steps": [{"stepIndex": <number>, "easyRead": "<text>"}]}."""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "items": {
        "type": "object",
        "properties": {"stepIndex": {"type": "integer"}, "easyRead": {"type": "string"}},
        "required": ["stepIndex", "easyRead"],
    }}},
    "required": ["steps"],
}


class OllamaUnavailable(Exception):
    """Ollama couldn't be reached, timed out, or answered with something that isn't the expected JSON."""


class OllamaClient:
    def __init__(self, s, http: httpx.Client | None = None):
        self._s = s
        self._http = http or httpx.Client()

    def rewrite(self, steps: list[dict]) -> dict[int, str]:
        question = {"steps": [{k: step.get(k) for k in ("stepIndex", "mode", "text", "keep")} for step in steps]}
        body = {
            "model": self._s.ollama_model, "stream": False, "format": ANSWER_SCHEMA,
            "keep_alive": self._s.ollama_keep_alive, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": json.dumps(question)}],
        }
        try:
            r = self._http.post(f"{self._s.ollama_url}/api/chat", json=body, timeout=self._s.ollama_timeout_s)
            r.raise_for_status()
            answer = json.loads(r.json()["message"]["content"])
            got = [(int(s["stepIndex"]), str(s["easyRead"])) for s in answer["steps"]
                   if isinstance(s, dict) and "stepIndex" in s and "easyRead" in s]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise OllamaUnavailable(type(e).__name__) from e
        # A small model sometimes copies the example's stepIndex (7) instead of the real one, e.g. for a
        # one-step route. Same number of steps but numbered wrong: match them up by position instead.
        # hallucination_check.py still checks each text against its own step's facts.
        wanted = [step["stepIndex"] for step in steps]
        if len(got) == len(wanted) and {i for i, _ in got} != set(wanted):
            return {i: text for i, (_, text) in zip(wanted, got)}
        return dict(got)

    def ready(self) -> bool:
        """Ollama answers and the model is pulled. A name without a tag means ":latest" to Ollama."""
        want = self._s.ollama_model if ":" in self._s.ollama_model else f"{self._s.ollama_model}:latest"
        try:
            r = self._http.get(f"{self._s.ollama_url}/api/tags", timeout=3)
            r.raise_for_status()
            return any(m.get("name") == want for m in r.json().get("models", []))
        except (httpx.HTTPError, ValueError):
            return False
