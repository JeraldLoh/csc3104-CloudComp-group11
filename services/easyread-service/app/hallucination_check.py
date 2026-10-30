"""Checks the AI's wording for each step before anyone sees it.

    problems(ai_text, step, max_chars) -> list[str]   why the AI text can't be used (empty list = it can)
    checked(steps, ai, max_chars)      -> list[dict]  per step: the AI text, or the step's own template text

A small model sometimes drops a stop name or makes up a bus number. For the people CareRoute is for, a wrong
instruction is worse than a plain one, so any step that fails falls back to the template text it was given
(journey-service's easy_read(), built only from the route's facts):
  - every `keep` string (bus/line number, stop names, stop count, [place N] tokens) must still be there;
  - every number in the AI text must already be in the step (no invented bus, stop or exit numbers);
  - the text must be non-empty and not longer than the app's text box.
"""
import re

NUMBER = re.compile(r"\d+")


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
    allowed = set(NUMBER.findall(step.get("text") or "")).union(*(NUMBER.findall(k) for k in keep))
    invented = sorted(set(NUMBER.findall(text)) - allowed)
    if invented:
        found.append(f"numbers not in the step: {invented}")
    return found


def checked(steps: list[dict], ai: dict[int, str], max_chars: int) -> list[dict]:
    """`ai` maps stepIndex -> AI text (a step the AI skipped is missing). Keeps the request's step order."""
    out = []
    for step in steps:
        text = (ai.get(step["stepIndex"]) or "").strip()
        bad = problems(text, step, max_chars)
        out.append({"stepIndex": step["stepIndex"], "easyRead": step["text"] if bad else text,
                    "fallback": bool(bad), "problems": bad})
    return out
