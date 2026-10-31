"""AI easy-read: simpler wording from the Easy-read Service (services/easyread-service, which runs Ollama).

    ai_request(steps, landmarks)                    -> (request body, {token: real name})
    suggest(steps, http, url, timeout, landmarks)   -> {"steps": [{stepIndex, easyRead, fallback, landmarks}], "provider"}

Only for a caregiver to review: suggest() saves nothing, and the caregiver app puts the text in the route editor.

Privacy (contracts/api.md: never names, addresses or labels like "Home"): a place without a stop code is a name
someone typed or an address, so it is sent as "[place N]" and put back in the answer. Public stop and station
names, bus numbers and stop counts are sent, and listed in `keep` so the service can check the AI kept them.
The AI rewrites the template wording from easy_read(), never text a caregiver wrote.

Landmarks (landmarks.py: {stepIndex: {spot: [landmark]}}) go with each step as `spots`: names, kinds, sides and
distances, never coordinates. The service lets the model look them up and checks that every landmark it names is
one it was given for that step; the ones it used come back as ids and are returned here with their names.

If the service is not configured, down, slow or answers badly, every step gets easy_read()'s wording and
`provider: "template"`, so planning, saving and trips never depend on it.
"""
import logging
import re

import httpx

from .easy_read import LINE_NAMES, easy_read, plain, stop_count

log = logging.getLogger("journey")

TOKEN = re.compile(r"\[place (\d+)\]", re.IGNORECASE)
SPOT_FIELDS = ("id", "name", "kind", "metres", "side")      # what the AI is told about a landmark: no coordinates
Landmarks = dict[int, dict[str, list[dict]]]          # {stepIndex: {spot: [landmark]}}, from landmarks.offer()


def ai_request(steps: list[dict], landmarks: Landmarks | None = None) -> tuple[dict, dict[str, str]]:
    tokens: dict[str, str] = {}                                    # real name -> "[place N]"

    def public(point: dict | None) -> dict:
        point = point or {}
        name = point.get("name")
        if not name or point.get("code"):
            return point
        return {**point, "name": tokens.setdefault(name, f"[place {len(tokens) + 1}]")}

    sent = []
    for step in steps:
        # The instruction is left out: it repeats the names (e.g. "Walk 200 m to Home"); only the count is needed.
        safe = {**step, "from": public(step.get("from")), "to": public(step.get("to")),
                "stopCount": stop_count(step), "instruction": ""}
        # What the template text names, so it must survive the rewrite. A walk's text only names where it ends.
        mode, line, count = step.get("mode"), step.get("line"), safe["stopCount"]
        ends = [safe["to"]] if mode == "walk" else [safe["from"], safe["to"]]
        keep = [plain(p["name"]) for p in ends if p.get("name")]
        if mode == "bus" and line:
            keep.insert(0, line)
        if mode == "train" and line in LINE_NAMES:
            keep.insert(0, LINE_NAMES[line])
        if mode in ("bus", "train") and count is not None:
            keep.append(str(count))
        out = {"stepIndex": step["stepIndex"], "mode": mode, "text": easy_read(safe), "keep": list(dict.fromkeys(keep))}
        if spots := (landmarks or {}).get(step["stepIndex"]):
            out["spots"] = {spot: [{k: b[k] for k in SPOT_FIELDS} for b in found] for spot, found in spots.items()}
        sent.append(out)
    return {"steps": sent}, {token: name for name, token in tokens.items()}


def _restore(text: str, names: dict[str, str]) -> str:
    return TOKEN.sub(lambda m: names.get(f"[place {m.group(1)}]", m.group(0)), text)


def suggest(steps: list[dict], http: httpx.Client, url: str, timeout: float,
            landmarks: Landmarks | None = None) -> dict:
    template = [{"stepIndex": s["stepIndex"], "easyRead": easy_read(s), "fallback": True, "landmarks": []} for s in steps]
    if not url:
        return {"steps": template, "provider": "template"}
    body, names = ai_request(steps, landmarks)
    offered = {i: {b["id"]: b for found in spots.values() for b in found} for i, spots in (landmarks or {}).items()}
    try:
        r = http.post(f"{url.rstrip('/')}/internal/easyread", json=body, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        answers = {int(s["stepIndex"]): s for s in data["steps"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        log.warning("easy-read service unavailable (%s): using the template wording", type(e).__name__)
        return {"steps": template, "provider": "template"}
    out = []
    for fallback in template:
        got = answers.get(fallback["stepIndex"]) or {}
        text = got.get("easyRead")
        if isinstance(text, str) and text.strip():
            mine = offered.get(fallback["stepIndex"], {})             # only this step's (the service checks too)
            used = [mine[i] for i in got.get("landmarks") or [] if i in mine]
            out.append({"stepIndex": fallback["stepIndex"], "easyRead": _restore(text, names),
                        "fallback": bool(got.get("fallback")),
                        "landmarks": [{k: b.get(k) for k in ("name", "kind", "lat", "lng", "seenFrom")} for b in used]})
        else:                                                       # no usable answer for this step
            out.append(fallback)
    return {"steps": out, "provider": data.get("provider") or "template"}
