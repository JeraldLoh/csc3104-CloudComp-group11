"""Checks the AI's wording for each step before anyone sees it.

    problems(ai_text, step, max_chars) -> list[str]   why the AI text can't be used (empty list = it can)
    checked(steps, answer, max_chars)  -> list[dict]  per step: the AI text, or the step's own template text

A small model sometimes drops a stop name or makes up a bus number. For the people CareRoute is for, a wrong
instruction is worse than a plain one, so any step that fails falls back to the template text it was given
(journey-service's easy_read(), built only from the route's facts, plus the landmark sentences ollama_client.py
placed in it):
  - every `keep` string (bus/line number, stop names, stop count, [place N] tokens, landmarks) must still be there;
  - every number in the AI text must already be in the step (no invented bus, stop or exit numbers);
  - no other names: a capitalised word inside a sentence must already be in the step (no invented landmark);
  - the text must be non-empty and not longer than the app's text box.
"""
import re

NUMBER = re.compile(r"\d+")
WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
SENTENCE = re.compile(r"(?<=[.!?])\s+")
# Capitalised words that are fine anywhere, though not in the step's own text.
COMMON = {"i", "you", "mrt", "lrt", "bus", "stop", "station", "exit", "interchange", "line", "platform", "bell"}


def _names(text: str) -> set[str]:
    """Capitalised words that don't start a sentence: names."""
    found = set()
    for sentence in SENTENCE.split(text):
        words = WORD.findall(sentence)
        found |= {w.lower() for w in words[1:] if w[0].isupper()}
    return found


def problems(ai_text: str | None, step: dict, max_chars: int) -> list[str]:
    text = (ai_text or "").strip()
    if not text:
        return ["no text"]
    found = []
    if len(text) > max_chars:
        found.append(f"longer than {max_chars} characters")
    keep = [k for k in step.get("keep") or [] if k]
    missing = [k for k in keep if k.lower() not in text.lower()]
    if missing:
        found.append(f"dropped {missing}")
    sources = [step.get("text") or "", *keep]
    allowed = set().union(*(NUMBER.findall(source) for source in sources))
    invented = sorted(set(NUMBER.findall(text)) - allowed)
    if invented:
        found.append(f"numbers not in the step: {invented}")
    vocabulary = COMMON | {w.lower() for source in sources for w in WORD.findall(source)}
    strangers = sorted(_names(text) - vocabulary)
    if strangers:
        found.append(f"names not in the step: {strangers}")
    return found


def checked(steps: list[dict], answer, max_chars: int) -> list[dict]:
    """`answer`: ollama_client.Answer, or None when Ollama was down. Checks against the steps the AI rewrote (with
    their landmark sentences) and keeps the request's step order. A step's landmarks are in its text either way."""
    if answer is not None:
        steps = answer.steps
    out = []
    for step in steps:
        text = ((answer.texts.get(step["stepIndex"]) if answer else None) or "").strip()
        bad = problems(text, step, max_chars)
        out.append({"stepIndex": step["stepIndex"], "easyRead": step["text"] if bad else text, "fallback": bool(bad),
                    "landmarks": step.get("landmarks") or [], "problems": bad})
    return out
