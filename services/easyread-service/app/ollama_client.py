"""Talks to Ollama (https://github.com/ollama/ollama/blob/main/docs/api.md).

    OllamaClient.rewrite(steps) -> Answer(texts, steps)   the AI wording for the whole route
    OllamaClient.ready()        -> bool                   Ollama answers and has the model pulled

Without landmarks it is one /api/chat call: the model rewrites each step's template text. With them (steps carrying
`spots`, see main.py) the model first finds and chooses landmarks, in small jobs a small model does reliably:

  1. look up   it gets a `nearby` tool and, for up to `ollama_tool_rounds` rounds, asks which buildings are at a
               step's spots. Each answer is read from the step's own `spots`, never from anywhere else. A spot it
               didn't ask about is still offered in job 2 (a small model often asks for one spot a round, or stops
               early), so every step can get a landmark.
  2. choose    it picks the landmark the traveller can't miss at each spot, or "none". The answer's JSON schema has
               one key per spot, each allowing only that spot's own names, so it can't make one up or move one.
  3. place     code adds a sentence for each pick where it is seen ("Look out for City Plaza on your left. Press the
               bell." before getting off a bus), and the names join the step's `keep`.
  4. rewrite   the usual rewrite of the (now longer) template text, checked by hallucination_check.py; a step that
               fails gets the template text with the landmark sentences, so the landmarks are kept either way.

Asking one model call to do all of this at once failed: a 1.5B model mixed up steps and made landmarks up.
Answers are forced into JSON by `format`, with temperature 0 and a fixed seed so they are repeatable, and all calls together get
`ollama_timeout_s`.
"""
import json
import logging
import re
import time
from typing import NamedTuple

import httpx

log = logging.getLogger("easyread")

SYSTEM_PROMPT = """You rewrite public transport instructions in Easy Read English for adults with \
intellectual disabilities who travel alone in Singapore.

The app shows every sentence as its own bullet point, so write a short checklist.

Rules:
- One action per sentence. Never join two actions with "and" or a comma.
- Start every sentence with a verb, for example Go, Walk, Wait, Get on, Tap, Count, Take, Get off, Look for.
- At most 6 words per sentence, not counting stop, station and landmark names.
- Speak to the reader as "you". Use present tense. Use common, short words.
- No idioms, no abbreviations, no brackets apart from the [place N] tokens.
- Copy every bus number, stop name, station name, landmark, number and [place N] token exactly as written.
- Keep every landmark in the same place in the step, with its side (left or right).
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

# With the writing rules as well, a small model skips the tool and writes (and makes up) landmarks straight away.
LOOKUP_PROMPT = """You look up landmarks along a public transport route in Singapore, so a traveller can check they \
are going the right way. Each step lists its spots. Call the nearby tool for every spot of every step. \
Do not write anything else."""
LOOKUP_TOKENS = 200                 # a round of tool calls is short; a model that starts writing instead is cut off

CHOOSE_PROMPT = """You choose landmarks for a traveller with an intellectual disability, so they can check they are going the right way. You get the buildings the traveller will see at each spot of their route ("start" and "end" of a walk or bus ride, "middle" of a walk, "before_stop": a little before getting off a bus).

For each spot, choose the one building the traveller can't miss: a mall, an MRT station, a bus interchange, a hawker centre, a school, a park, a hospital, or a big well-known building. Answer "none" if the spot only has plain buildings and "none" is allowed. Answer with JSON only, one key per spot."""

NEARBY_TOOL = {"type": "function", "function": {
    "name": "nearby",
    "description": "The named buildings the traveller will see at one spot of a step, closest first.",
    "parameters": {"type": "object", "required": ["stepIndex", "spot"], "properties": {
        "stepIndex": {"type": "integer", "description": "The step's stepIndex."},
        "spot": {"type": "string", "enum": ["start", "middle", "end", "before_stop"]}}},
}}

PER_STEP = 2                        # landmarks a step may get: more makes the text too long to follow
PLAIN = {None, "building"}          # kinds (from journey-service) that may not be worth mentioning


def _choice_schema(options: dict[str, list[dict]]) -> dict:
    """One key per spot, each allowed only that spot's own names: the model must think about every spot, and can't
    pick a building from somewhere else. "none" only where there are only plain buildings (offered everywhere, a
    small model answers "none" for everything)."""
    return {"type": "object", "required": list(options), "properties": {key: {"type": "string", "enum": [
        *(b["name"] for b in found), *(["none"] if all(b.get("kind") in PLAIN for b in found) else [])]}
        for key, found in options.items()}}


def _on(side: str | None) -> str:
    return f" on your {side}" if side in ("left", "right") else ""


def place(step: dict, picks: list[tuple[str, dict]]) -> dict:
    """The step with a sentence for each (spot, landmark) where it is seen, its names in `keep` and its ids in
    `landmarks`. Bus: before_stop just before getting off, end just after. Walk: start first, middle and end last."""
    if not picks:
        return {**step, "landmarks": []}
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", step["text"].strip()) if s]
    get_off = next((i for i, s in enumerate(sentences) if s.lower().startswith("get off")), len(sentences))
    before, after, first = [], [], []
    for spot, b in picks:
        name, on = b["name"], _on(b.get("side"))
        if spot == "before_stop":
            before += [f"Look out for {name}{on}.", "Press the bell."]
        elif spot == "start":
            first.append(f"Look for {name}{on}.")
        elif spot == "middle":
            after.append(f"Walk past {name}{on}.")
        else:                                                        # end: where the bus stops or the walk ends
            after.append(f"Look for {name}{on}.")
    if step.get("mode") == "bus":
        sentences = sentences[:get_off] + before + sentences[get_off:get_off + 1] + after + sentences[get_off + 1:]
    else:
        sentences = first + sentences + after
    keep = list(dict.fromkeys([*(step.get("keep") or []), *(b["name"] for _, b in picks)]))
    return {**step, "text": " ".join(sentences), "keep": keep, "landmarks": [b["id"] for _, b in picks]}


class OllamaUnavailable(Exception):
    """Ollama couldn't be reached, timed out, or answered with something that isn't the expected JSON."""


class Answer(NamedTuple):
    texts: dict[int, str]           # stepIndex -> AI text (a step the AI skipped is missing)
    steps: list[dict]               # the steps it rewrote: with landmark sentences, `keep` and `landmarks` added


class OllamaClient:
    def __init__(self, s, http: httpx.Client | None = None, clock=time.monotonic):
        self._s = s
        self._http = http or httpx.Client()
        self._clock = clock

    def rewrite(self, steps: list[dict]) -> Answer:
        deadline = self._clock() + self._s.ollama_timeout_s
        try:
            if self._s.landmarks != "off" and any(step.get("spots") for step in steps):
                steps = self._with_landmarks(steps, deadline)
            question = {"steps": [{k: step.get(k) for k in ("stepIndex", "mode", "text", "keep")} for step in steps]}
            reply = self._chat([{"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": json.dumps(question)}], deadline, format=ANSWER_SCHEMA)
            answer = json.loads(reply["content"])
            got = [(int(s["stepIndex"]), str(s["easyRead"])) for s in answer["steps"]
                   if isinstance(s, dict) and "stepIndex" in s and "easyRead" in s]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise OllamaUnavailable(type(e).__name__) from e
        # A small model sometimes copies the example's stepIndex (7) instead of the real one, e.g. for a
        # one-step route. Same number of steps but numbered wrong: match them up by position instead.
        # hallucination_check.py still checks each text against its own step's facts.
        wanted = [step["stepIndex"] for step in steps]
        if len(got) == len(wanted) and {i for i, _ in got} != set(wanted):
            return Answer({i: text for i, (_, text) in zip(wanted, got)}, steps)
        return Answer(dict(got), steps)

    def _with_landmarks(self, steps: list[dict], deadline: float) -> list[dict]:
        """Jobs 1-3. Best-effort: if the model fails at them, the steps are rewritten without landmarks."""
        try:
            picks = self._choose(self._look_up(steps, deadline), deadline)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("landmarks not chosen (%s): rewording without them", type(e).__name__)
            picks = {}
        return [place(step, picks.get(step["stepIndex"], [])) for step in steps]

    def _look_up(self, steps: list[dict], deadline: float) -> dict[int, dict[str, list[dict]]]:
        """Job 1: the model's own lookups, then every spot it skipped: what it was shown, {stepIndex: {spot: [...]}}.
        (Job 2 gets these as a list, not the conversation: a small model loses track of a long one.)"""
        spots = {step["stepIndex"]: step.get("spots") or {} for step in steps}
        question = {"steps": [{"stepIndex": step["stepIndex"], "mode": step.get("mode"), "text": step["text"],
                               "spots": list(spots[step["stepIndex"]])} for step in steps if spots[step["stepIndex"]]]}
        messages = [{"role": "system", "content": LOOKUP_PROMPT}, {"role": "user", "content": json.dumps(question)}]
        shown: dict[int, dict[str, list[dict]]] = {}

        def show(index: int, spot: str) -> list[dict]:
            found = spots[index][spot]
            shown.setdefault(index, {})[spot] = found
            return [{k: b.get(k) for k in ("name", "kind", "metres", "side")} for b in found]

        rounds = self._s.ollama_tool_rounds if self._s.landmarks == "tools" else 0
        for _ in range(rounds):
            reply = self._chat(messages, deadline, limit=LOOKUP_TOKENS, tools=[NEARBY_TOOL])
            calls = reply.get("tool_calls") or []
            if not calls:
                break
            messages.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": calls})
            for call in calls:
                args = (call.get("function") or {}).get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args or "{}")
                if isinstance(args.get("object"), dict):          # qwen2.5:3b wraps them: {"object": {...}}
                    args = args["object"]
                try:
                    index, spot = int(args.get("stepIndex")), str(args.get("spot"))
                except (TypeError, ValueError):
                    result = {"error": "give a stepIndex and a spot"}
                else:
                    result = (show(index, spot) or "nothing named here") if spot in spots.get(index, {}) else {
                        "error": f"step {index} has no spot {spot!r}; its spots are {list(spots.get(index, {}))}"}
                messages.append({"role": "tool", "tool_name": "nearby", "content": json.dumps(result)})
        for index, by_spot in spots.items():
            for spot in by_spot:
                shown.setdefault(index, {}).setdefault(spot, by_spot[spot])        # what it didn't ask about
        return shown

    def _choose(self, shown: dict[int, dict[str, list[dict]]], deadline: float) -> dict[int, list[tuple[str, dict]]]:
        """Job 2: the landmark to mention at each spot, {stepIndex: [(spot, landmark)]}, at most PER_STEP a step."""
        spots = {f"step {i} {spot}": (i, spot, found) for i, by_spot in sorted(shown.items())
                 for spot, found in by_spot.items() if found}
        if not spots:
            return {}
        listed = {key: [f"{b['name']} ({b.get('kind') or 'building'}, {b.get('metres')} m"
                        f"{', on the ' + b['side'] if b.get('side') else ''})" for b in found]
                  for key, (_, _, found) in spots.items()}
        schema = _choice_schema({key: found for key, (_, _, found) in spots.items()})
        reply = self._chat([{"role": "system", "content": CHOOSE_PROMPT},
                            {"role": "user", "content": json.dumps(listed)}], deadline, format=schema)
        chosen = json.loads(reply["content"])
        picks: dict[int, list[tuple[str, dict]]] = {}
        for key, (index, spot, found) in spots.items():
            b = next((b for b in found if b["name"] == chosen.get(key)), None)     # "none", or not this spot's
            mine = picks.setdefault(index, [])
            if b and len(mine) < PER_STEP and all(other["name"] != b["name"] for _, other in mine):
                mine.append((spot, b))
        return picks

    def _chat(self, messages: list[dict], deadline: float, limit: int | None = None, **extra) -> dict:
        left = deadline - self._clock()
        if left <= 0:
            raise httpx.TimeoutException("out of time")
        r = self._http.post(f"{self._s.ollama_url}/api/chat", timeout=left, json={
            "model": self._s.ollama_model, "stream": False, "keep_alive": self._s.ollama_keep_alive,
            "options": {"temperature": 0, "seed": 1, **({"num_predict": limit} if limit else {})}, "messages": messages, **extra})
        r.raise_for_status()
        return r.json()["message"]

    def ready(self) -> bool:
        """Ollama answers and the model is pulled. A name without a tag means ":latest" to Ollama."""
        want = self._s.ollama_model if ":" in self._s.ollama_model else f"{self._s.ollama_model}:latest"
        try:
            r = self._http.get(f"{self._s.ollama_url}/api/tags", timeout=3)
            r.raise_for_status()
            return any(m.get("name") == want for m in r.json().get("models", []))
        except (httpx.HTTPError, ValueError):
            return False
