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
         HarbourFront", worked out from the station codes); and which stop it is.
  walk   what is at the end (a bus stop, a station), the line to change to inside a station, and arriving.
Bus wording leaves out its least needed sentences (how often, then how long) to stay within MAX_CHARS, the
caregiver app's and the Easy-read Service's limit, so the traveller app's main button stays on screen.

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

# For the wording: each line as its signs show it (colour, name) and its two ends, for "going to ...". The ends
# are the lines' termini as of 2026; update them when a line is extended. LRT loops have no direction.
TRAIN_SIGNS = {
    "NS": ("red", "North South Line", "Jurong East", "Marina South Pier"),
    "EW": ("green", "East West Line", "Pasir Ris", "Tuas Link"),
    "CG": ("green", "East West Line", "Tanah Merah", "Changi Airport"),
    "NE": ("purple", "North East Line", "HarbourFront", "Punggol Coast"),
    "CC": ("yellow", "Circle Line", "Dhoby Ghaut", "HarbourFront"),
    "DT": ("blue", "Downtown Line", "Bukit Panjang", "Expo"),
    "TE": ("brown", "Thomson-East Coast Line", "Woodlands North", "Bayshore"),
}
STATION_CODE = re.compile(r"^([A-Z]+)(\d+)")
MAX_CHARS = 300                     # the caregiver app's (MAX_EASY_READ) and the Easy-read Service's limit

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


def _station(point: dict | None) -> bool:
    code = str((point or {}).get("code") or "")
    return bool(code) and not code.isdigit()


def _going_to(step: dict) -> str | None:
    """The end of the line the train is heading for, from the first and last station codes of the ride."""
    stops = [s.get("code") or "" for s in step.get("stops") or []] or [(step.get("from") or {}).get("code") or "",
                                                                       (step.get("to") or {}).get("code") or ""]
    first, last = STATION_CODE.match(stops[0]), STATION_CODE.match(stops[-1])
    if not (first and last) or first.group(1) != last.group(1) or first.group(1) not in TRAIN_SIGNS:
        return None
    low, high = TRAIN_SIGNS[first.group(1)][2:]
    return high if int(last.group(2)) > int(first.group(2)) else low if int(last.group(2)) < int(first.group(2)) else None


def _line_signs(code: str | None, line: str | None = None) -> str:
    """'the purple signs for the North East Line', from a station code (NE17) or else the line (NE)."""
    found = STATION_CODE.match(code or "")
    colour, name = _line_colour_name(f"{found.group(1) if found else line}1")
    return f"the {colour} signs for the {name}" if colour else f"the signs for the {name}"


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
    frm, to = step.get("from") or {}, step.get("to") or {}
    start, end = plain(frm.get("name")), plain(to.get("name"))
    line, mode = step.get("line"), step.get("mode")
    count = stop_count(step)
    minutes = math.ceil((step.get("expectedDurationS") or 0) / 60)
    about = f"It takes about {minutes} minutes." if minutes >= 2 else None

    if mode == "bus" and line:
        code = frm.get("code") if str(frm.get("code") or "").isdigit() else None
        stops = step.get("stops") or []
        before = plain(stops[-2].get("name")) if len(stops) >= 3 else None       # the stop before getting off
        towards, every = plain(step.get("towards")), step.get("every")
        end = end or "your stop"
        parts = [(f"Find bus stop {code} at {start}." if code and start else f"Go to {start}." if start else "", 0),
                 ("Check the number on the pole." if code else "", 0),
                 (f"Raise your hand for bus {line} to {towards}." if towards else f"Raise your hand for bus {line}.", 0),
                 ((f"It comes every {every[0]} minutes." if every[0] == every[1]
                   else f"It comes every {every[0]} to {every[1]} minutes.") if every else "", 2),
                 ("Tap your card to get on.", 0),
                 (f"The ride takes about {minutes} minutes." if minutes >= 2 else "", 1)]
        if count == 1:
            parts += [("Press the bell when the bus moves off.", 0), (f"Get off at the next stop, {end}.", 0)]
        elif before:
            parts += [(f"After {before}, press the bell.", 0), (f"Get off at {end}.", 0)]
        else:
            parts += [(f"Get off at {end}.", 0), (f"It is the {ordinal(count)} stop." if count else "", 0)]
        parts.append(("Tap your card to get off.", 0))
        return _fit([(t, p) for t, p in parts if t])
    if mode == "train":
        going = _going_to(step)
        text = [] if prev and prev.get("mode") == "walk" and _station(prev.get("from")) and _station(prev.get("to")) \
            else ["Tap your card at the gantry."]                                   # changed lines: already inside
        text += [f"Follow {_line_signs(frm.get('code'), line)}."]
        text += [f"Take the train going to {going}." if going else "Get on the train."]
        text += [f"Get off at {end or 'your station'}."]
        text += [f"It is the {ordinal(count)} stop."] if count else []
        return " ".join(text)
    if mode == "walk":
        if _station(frm) and _station(to):                                          # a change of line inside a station
            colour, name = _line_colour_name(to.get("code"))
            return " ".join([f"Change to the {colour} {name}." if colour else f"Change to the {name}.",
                             f"Follow the {colour} signs." if colour else "Follow the signs.", *([about] if about else [])])
        if last and not to.get("code"):
            return " ".join(t for t in (f"Walk to {plain(destination) or end or 'your destination'}.", about,
                                        "You have arrived.") if t)
        where = f"the bus stop at {end}" if end and str(to.get("code") or "").isdigit() else end or "your destination"
        return " ".join(t for t in (f"Walk to {where}.", about) if t)
    return step.get("instruction") or ""


def _line_colour_name(code: str | None) -> tuple[str | None, str]:
    """("red", "North South Line") for NS24; no colour for an LRT or an unknown code."""
    found = STATION_CODE.match(code or "")
    key = found.group(1) if found else ""
    if key in TRAIN_SIGNS:
        return TRAIN_SIGNS[key][0], TRAIN_SIGNS[key][1]
    return None, LINE_NAMES.get(key, "other line")


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
