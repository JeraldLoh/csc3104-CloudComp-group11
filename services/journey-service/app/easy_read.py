"""Easy-read wording: short plain-language text for a route step.

    easy_read(step, prev, last, destination) -> str; built only from the route's facts (no AI, so nothing can be
                              invented): stop codes, bus and line numbers, names and counts are copied from the
                              route. One short action per sentence, starting with a verb: the apps show each
                              sentence as a bullet point ("Find bus stop 65009 at Block 119D.", ...)
    route_wording(steps, destination) -> each step's wording, knowing the steps around it
    with_easy_read(route)  -> the route with easy-read wording filled in for steps that have none

What each part is for, beyond saying where to go (the mistakes it heads off):
  bus    the stop's number, on its pole (the stop across the road has the same name but goes the other way);
         raise a hand for the bus going where its front sign says (buses here only stop when someone waits or
         rings), and how often it comes; tap the card when getting off too (fares are by distance); how long
         the ride is; and the stop before getting off as the cue to press the bell, rather than counting stops
         (a bus skips a stop where nobody waits or rings).
  train  tap in at the gantry; the line's sign colour and name; which way, as station signs say it ("going to
         HarbourFront", worked out from the station codes, or the next station); and which stop it is.
  walk   what is at the end (a bus stop, a station), the line to change to inside a station, and arriving.
Bus wording leaves out its least needed sentences (how often, then how long) to stay within
MAX_CHARS, the caregiver app's and the Easy-read Service's limit, so the traveller app's main button stays on
screen. A step whose essential sentences alone are longer (very long stop names) is left longer.

The AI rewrite (easy_read_ai.py -> the Easy-read Service) starts from this text and falls back to it.
"""
import math
import os
import re

LINE_NAMES = {
    "NS": "North South Line", "EW": "East West Line", "NE": "North East Line",
    "CC": "Circle Line", "DT": "Downtown Line", "TE": "Thomson-East Coast Line",
    "CG": "East West Line (Changi branch)", "BP": "Bukit Panjang LRT",
    "SE": "Sengkang LRT", "SW": "Sengkang LRT", "PE": "Punggol LRT", "PW": "Punggol LRT",
}

# For the wording: each line as its signs show it (colour, name) and its two ends, for "going to ...". The ends
# are the lines' termini as of 2026; update them when a line is extended. A line without them (the Circle Line, the
# LRT loops) is told by the station the train stops at next, which the route itself says.
TRAIN_SIGNS = {
    "NS": ("red", "North South Line", "Jurong East", "Marina South Pier"),
    "EW": ("green", "East West Line", "Pasir Ris", "Tuas Link"),
    "CG": ("green", "East West Line", "Tanah Merah", "Changi Airport"),
    "NE": ("purple", "North East Line", "HarbourFront", "Punggol Coast"),
    "CC": ("yellow", "Circle Line", None, None),   # not two simple ends (Marina Bay branch, closing into a loop)
    "DT": ("blue", "Downtown Line", "Bukit Panjang", "Expo"),
    "TE": ("brown", "Thomson-East Coast Line", "Woodlands North", "Bayshore"),
}
STATION_CODE = re.compile(r"^([A-Z]+)(\d+)")
# The longest wording for one step (EASYREAD_MAX_CHARS), the one setting for it: the Easy-read Service is sent it
# with each request (easy_read_ai.py) and the caregiver app reads it from GET /api/easy-read/limits.
MAX_CHARS = int(os.getenv("EASYREAD_MAX_CHARS", "300"))

# Short forms in LTA stop names, spelled out for the plain-language text only (longest first).
EASY_WORDS = [(r"\bPr Sch\b", "Primary School"), (r"\bSec Sch\b", "Secondary School"), (r"\bOpp\b", "Opposite"),
              (r"\bStn\b", "Station"), (r"\bBef\b", "Before"), (r"\bAft\b", "After"), (r"\bBlk\b", "Block"),
              (r"\bInt\b", "Interchange"), (r"\bSch\b", "School"), (r"\bMkt/Fc\b", "Market and Food Centre"),
              (r"\bMkt\b", "Market"), (r"\bFc\b", "Food Centre"), (r"\bCtr\b", "Centre"), (r"\bCtrl\b", "Central"),
              (r"\bNth\b", "North"), (r"\bSth\b", "South"), (r"\bAve\b", "Avenue"), (r"\bRd\b", "Road"),
              (r"\bHosp\b", "Hospital"), (r"\bTer\b", "Terminal")]


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


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def is_station(point: dict | None) -> bool:
    """It has a station code (NE16, CC13); bus stop codes are all digits."""
    code = str((point or {}).get("code") or "")
    return bool(code) and not code.isdigit()


def changes_line(step: dict) -> bool:
    """A walk from one station platform to another (Dhoby Ghaut NE6 -> NS24), inside the station."""
    return step.get("mode") == "walk" and is_station(step.get("from")) and is_station(step.get("to"))


def _minutes(step: dict) -> int:
    return math.ceil((step.get("expectedDurationS") or 0) / 60)


def _line_key(code: str | None) -> str:
    """'NE' for the station code NE17; '' for a bus stop or no code."""
    found = STATION_CODE.match(code or "")
    return found.group(1) if found else ""


def _line_colour_name(key: str) -> tuple[str | None, str]:
    """("red", "North South Line") for NS; no colour for an LRT or an unknown line."""
    if key in TRAIN_SIGNS:
        return TRAIN_SIGNS[key][0], TRAIN_SIGNS[key][1]
    return None, LINE_NAMES.get(key, "other line")


def _line_signs(code: str | None, line: str | None = None) -> str:
    """'the purple signs for the North East Line', from a station code (NE17) or else the line (NE)."""
    colour, name = _line_colour_name(_line_key(code) or line or "")
    return f"the {colour} signs for the {name}" if colour else f"the signs for the {name}"


def _going_to(step: dict) -> str | None:
    """The end of the line the train is heading for, from the first and last station codes of the ride."""
    codes = [s.get("code") or "" for s in step.get("stops") or []]
    if not codes:
        codes = [(step.get("from") or {}).get("code") or "", (step.get("to") or {}).get("code") or ""]
    first, last = STATION_CODE.match(codes[0]), STATION_CODE.match(codes[-1])
    if not (first and last) or first.group(1) != last.group(1) or first.group(1) not in TRAIN_SIGNS:
        return None
    low, high = TRAIN_SIGNS[first.group(1)][2:]
    start, end = int(first.group(2)), int(last.group(2))
    if start == end or not (low and high):
        return None
    return high if end > start else low


def _fit(parts: list[tuple[str, int]]) -> str:
    """The sentences, leaving out the optional ones (priority > 0, highest first) until the text fits MAX_CHARS."""
    for drop in sorted({p for _, p in parts if p}, reverse=True):
        if len(" ".join(t for t, _ in parts)) <= MAX_CHARS:
            break
        parts = [(t, p) for t, p in parts if p != drop]
    return " ".join(t for t, _ in parts)


def easy_read(step: dict, prev: dict | None = None, last: bool = False, destination: str | None = None) -> str:
    """Short plain-language wording for a step. `prev`: the step before (a train after a change of line needs no
    gantry); `last` and `destination`: the last step of a route ends at its destination, by name."""
    mode = step.get("mode")
    if mode == "bus" and step.get("line"):
        return _bus(step)
    if mode == "train":
        return _train(step, prev)
    if mode == "walk":
        return _walk(step, last, destination)
    return step.get("instruction") or ""


def _bus(step: dict) -> str:
    """Optional sentences carry a priority for _fit(): how often it comes goes first (2), then how long (1)."""
    frm = step.get("from") or {}
    start, end = plain(frm.get("name")), plain((step.get("to") or {}).get("name")) or "your stop"
    code = frm.get("code") if str(frm.get("code") or "").isdigit() else None
    line, count, minutes = step["line"], stop_count(step), _minutes(step)
    towards, every = plain(step.get("towards")), step.get("every")
    stops = step.get("stops") or []
    before = plain(stops[-2].get("name")) if len(stops) >= 3 else None           # the stop before getting off

    parts = []
    if code and start:
        parts.append((f"Find bus stop {code} at {start}.", 0))
    elif start:
        parts.append((f"Go to {start}.", 0))
    if code:
        parts.append(("Check the number on the pole.", 0))
    parts.append((f"Raise your hand for bus {line} to {towards}." if towards else f"Raise your hand for bus {line}.", 0))
    if every:
        low, high = every
        parts.append((f"It comes every {low} minutes." if low == high else f"It comes every {low} to {high} minutes.", 2))
    parts.append(("Tap your card to get on.", 0))
    if minutes >= 2:
        parts.append((f"The ride takes about {minutes} minutes.", 1))
    if count == 1:
        parts += [("Press the bell when the bus moves off.", 0), (f"Get off at the next stop, {end}.", 0)]
    elif before:
        parts += [(f"After {before}, press the bell.", 0), (f"Get off at {end}.", 0)]
    else:
        parts.append((f"Get off at {end}.", 0))
        if count:
            parts.append((f"It is the {ordinal(count)} stop.", 0))
    parts.append(("Tap your card to get off.", 0))
    return _fit(parts)


def _train(step: dict, prev: dict | None) -> str:
    end = plain((step.get("to") or {}).get("name")) or "your station"
    going, count = _going_to(step), stop_count(step)
    text = [] if prev and changes_line(prev) else ["Tap your card at the gantry."]  # changed lines: already inside
    text.append(f"Follow {_line_signs((step.get('from') or {}).get('code'), step.get('line'))}.")
    stops = step.get("stops") or []
    following = plain(stops[1].get("name")) if len(stops) >= 2 else None       # the station it stops at next
    if going:
        text.append(f"Take the train going to {going}.")
    elif following and len(stops) > 2:
        text.append(f"Take the train that stops next at {following}.")
    else:
        text.append("Get on the train.")
    text.append(f"Get off at {end}.")
    if count:
        text.append(f"It is the {ordinal(count)} stop.")
    return " ".join(text)


def _walk(step: dict, last: bool, destination: str | None) -> str:
    to = step.get("to") or {}
    end = plain(to.get("name"))
    minutes = _minutes(step)
    about = f"It takes about {minutes} minutes." if minutes >= 2 else None
    if changes_line(step):                                          # a change of line inside a station
        colour, name = _line_colour_name(_line_key(to.get("code")))
        text = [f"Change to the {colour} {name}." if colour else f"Change to the {name}.",
                f"Follow the {colour} signs." if colour else "Follow the signs.", about]
    elif last and not to.get("code"):
        text = [f"Walk to {plain(destination) or end or 'your destination'}.", about, "You have arrived."]
    else:
        where = f"the bus stop at {end}" if end and str(to.get("code") or "").isdigit() else end or "your destination"
        text = [f"Walk to {where}.", about]
    return " ".join(t for t in text if t)


def route_wording(steps: list[dict], destination: str | None = None) -> list[str]:
    """easy_read() for each step of a route, each knowing the step before it and whether it is the last."""
    return [easy_read(step, steps[i - 1] if i else None, i == len(steps) - 1, destination)
            for i, step in enumerate(steps)]


def with_easy_read(data: dict) -> dict:
    """Fills in easy-read wording for steps that have none; text a caregiver wrote is kept. Wording that is still
    the planner's own, made without the destination's name ("Walk to your destination."), is made again with it."""
    planned = route_wording(data["steps"])
    wording = route_wording(data["steps"], (data.get("destination") or {}).get("name"))
    steps = [{**s, "easyRead": text if not s.get("easyRead") or s["easyRead"] == old else s["easyRead"]}
             for s, old, text in zip(data["steps"], planned, wording)]
    return {**data, "steps": steps}
