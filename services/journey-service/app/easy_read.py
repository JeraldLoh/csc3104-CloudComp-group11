"""Easy-read wording: short plain-language text for a route step.

    easy_read(step)        -> str; built only from the step's own facts (no AI, so nothing can be invented):
                              the bus number, station names and stop count are copied from the route.
                              One short action per sentence, starting with a verb: the apps show each
                              sentence as a bullet point ("Go to ...", "Wait for bus number 190.", ...)
    with_easy_read(route)  -> the route with easy-read wording filled in for steps that have none

The AI rewrite (easy_read_ai.py -> the Easy-read Service) starts from this text and falls back to it.
"""
import math
import re

LINE_NAMES = {
    "NS": "North South Line", "EW": "East West Line", "NE": "North East Line",
    "CC": "Circle Line", "DT": "Downtown Line", "TE": "Thomson-East Coast Line",
    "CG": "East West Line (Changi branch)", "BP": "Bukit Panjang LRT",
    "SE": "Sengkang LRT", "SW": "Sengkang LRT", "PE": "Punggol LRT", "PW": "Punggol LRT",
}

# Short forms in LTA stop names, spelled out for the plain-language text only (longest first).
EASY_WORDS = [(r"\bPr Sch\b", "Primary School"), (r"\bSec Sch\b", "Secondary School"), (r"\bOpp\b", "Opposite"),
              (r"\bStn\b", "Station"), (r"\bBlk\b", "Block"), (r"\bInt\b", "Interchange"), (r"\bSch\b", "School")]


def plain(name: str | None) -> str | None:
    """A stop name with LTA's short forms spelled out, as the easy-read text shows it."""
    if not name:
        return name
    for pattern, full in EASY_WORDS:
        name = re.sub(pattern, full, name)
    return name


def stop_count(step: dict) -> int | None:
    """How many stops a bus/train step rides; older routes only have it inside the instruction."""
    if step.get("stopCount") is not None:
        return step["stopCount"]
    found = re.search(r"\((\d+) stops?\)", step.get("instruction") or "")
    return int(found.group(1)) if found else None


def easy_read(step: dict) -> str:
    """Short plain-language wording for a step."""
    start = plain((step.get("from") or {}).get("name"))
    end = plain((step.get("to") or {}).get("name")) or "your destination"
    line = step.get("line")
    count = stop_count(step)
    stops = None if count is None else f"{count} stop" + ("" if count == 1 else "s")
    minutes = math.ceil((step.get("expectedDurationS") or 0) / 60)

    go = [f"Go to {start}."] if start else []
    count_stops = [f"Count {stops}."] if stops else []
    if step.get("mode") == "bus" and line:
        text = [*go, f"Wait for bus number {line}.", "Get on the bus.", "Tap your card.", *count_stops,
                f"Get off at {end}."]
    elif step.get("mode") == "train":
        name = LINE_NAMES.get(line, f"{line} line") if line else "MRT"
        text = [*go, f"Take the {name} train.", *count_stops, f"Get off at {end}."]
    elif step.get("mode") == "walk":
        text = [f"Walk to {end}."] + ([f"It takes about {minutes} minutes."] if minutes >= 2 else [])
    else:
        return step.get("instruction") or ""
    return " ".join(text)


def with_easy_read(data: dict) -> dict:
    """Fills in easy-read wording for steps that have none; text a caregiver wrote is kept."""
    steps = [{**s, "easyRead": s.get("easyRead") or easy_read(s)} for s in data["steps"]]
    return {**data, "steps": steps}
