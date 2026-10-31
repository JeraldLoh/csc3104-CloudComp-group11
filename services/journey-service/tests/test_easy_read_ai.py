"""AI easy-read (easy_read_ai.py): private names never leave the service, and every failure gives template text."""
import json

import httpx
import respx

from app.easy_read import easy_read, route_wording
from app.easy_read_ai import ai_request, suggest

EASYREAD = "http://easyread.test:8000"

WALK = {"stepIndex": 0, "mode": "walk", "instruction": "Walk 200 m to Opp Blk 123", "expectedDurationS": 240,
        "from": {"name": "Home", "code": None}, "to": {"name": "Opp Blk 123", "code": "65009"}}
BUS = {"stepIndex": 1, "mode": "bus", "line": "190", "instruction": "Take bus 190 from Opp Blk 123 to Punggol Int (6 stops)",
       "from": {"name": "Opp Blk 123", "code": "65009"}, "to": {"name": "Punggol Int", "code": "65221"}}
LAST = {"stepIndex": 2, "mode": "walk", "instruction": "Walk 100 m to Grandma's flat", "expectedDurationS": 60,
        "from": {"name": "Punggol Int", "code": "65221"}, "to": {"name": "Grandma's flat", "code": None}}
STEPS = [WALK, BUS, LAST]


def test_request_hides_names_without_a_stop_code():
    body, names = ai_request(STEPS)
    sent = json.dumps(body)
    assert "Home" not in sent and "Grandma" not in sent
    assert names == {"[place 1]": "Home", "[place 2]": "Grandma's flat"}
    last = body["steps"][2]
    assert last["text"] == "Walk to [place 2]. You have arrived." and last["keep"] == ["[place 2]"]


def test_request_tells_the_service_the_limit():
    from app.easy_read import MAX_CHARS
    assert ai_request(STEPS)[0]["maxChars"] == MAX_CHARS


def test_request_names_the_destination_by_its_token():
    last = {**LAST, "to": {"name": None, "code": None}}            # how a planned route's last step ends
    body, names = ai_request([WALK, BUS, last], destination="Clinic")
    assert body["steps"][2]["text"] == "Walk to [place 2]. You have arrived." and names["[place 2]"] == "Clinic"
    assert "Clinic" not in json.dumps(body)


def test_request_keeps_the_facts_the_text_names():
    bus = ai_request(STEPS)[0]["steps"][1]
    # Short forms are spelled out, as in the text; the stop count comes from the instruction for older routes.
    # The count as the text says it ("6th"): a bare "6" would also be found inside the stop code 65009.
    assert bus["keep"] == ["65009", "190", "Opposite Block 123", "Punggol Interchange", "6th"]
    assert all(k in bus["text"] for k in bus["keep"])


@respx.mock
def test_suggest_puts_the_real_names_back():
    def answer(request):
        steps = json.loads(request.content)["steps"]
        return httpx.Response(200, json={"provider": "ollama", "model": "m", "steps": [
            {"stepIndex": s["stepIndex"], "easyRead": s["text"].upper() if s["mode"] == "walk" else s["text"],
             "fallback": s["mode"] != "walk"} for s in steps]})
    respx.post(f"{EASYREAD}/internal/easyread").mock(side_effect=answer)
    out = suggest(STEPS, httpx.Client(), EASYREAD, 5)
    assert out["provider"] == "ollama"
    assert out["steps"][2] == {"stepIndex": 2, "easyRead": "WALK TO Grandma's flat. YOU HAVE ARRIVED.", "fallback": False,
                                "landmarks": []}
    assert out["steps"][1]["fallback"] is True


@respx.mock
def test_suggest_falls_back_when_the_service_is_down_or_unset():
    respx.post(f"{EASYREAD}/internal/easyread").mock(side_effect=httpx.ConnectError("refused"))
    for url in (EASYREAD, ""):
        out = suggest(STEPS, httpx.Client(), url, 5)
        assert out["provider"] == "template"
        assert [s["easyRead"] for s in out["steps"]] == route_wording(STEPS)
        assert all(s["fallback"] for s in out["steps"])


@respx.mock
def test_suggest_keeps_template_for_a_step_the_service_left_out():
    respx.post(f"{EASYREAD}/internal/easyread").mock(return_value=httpx.Response(200, json={
        "provider": "ollama", "steps": [{"stepIndex": 0, "easyRead": "Walk to Opposite Block 123.", "fallback": False}]}))
    out = suggest(STEPS, httpx.Client(), EASYREAD, 5)
    assert out["steps"][0]["fallback"] is False
    assert out["steps"][1] == {"stepIndex": 1, "easyRead": easy_read(BUS), "fallback": True, "landmarks": []}
