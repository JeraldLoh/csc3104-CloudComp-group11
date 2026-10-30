"""The Easy-read Service with Ollama mocked (respx): the AI text is used only when it passes the checks."""
import json

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.hallucination_check import problems
from app.main import create_app

OLLAMA = "http://ollama.test:11434"
SETTINGS = Settings(ollama_url=OLLAMA, ollama_model="qwen2.5:1.5b", ollama_timeout_s=1)

BUS = {"stepIndex": 1, "mode": "bus", "keep": ["45", "Opposite Fengshan Primary School", "Tanah Merah Station Exit 2", "5"],
       "text": "At Opposite Fengshan Primary School, wait for bus number 45. Get on and tap your card. "
               "Get off at Tanah Merah Station Exit 2 after 5 stops."}
WALK = {"stepIndex": 2, "mode": "walk", "keep": ["[place 1]"], "text": "Walk to [place 1]. It takes about 4 minutes."}


def answer(*steps) -> httpx.Response:
    """What Ollama's /api/chat returns: the model's JSON as a string inside message.content."""
    content = json.dumps({"steps": [{"stepIndex": i, "easyRead": t} for i, t in steps]})
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}, "done": True})


def post(*steps):
    with TestClient(create_app(s=SETTINGS)) as c:
        return c.post("/internal/easyread", json={"steps": list(steps)})


# ----- the checks on their own -----

def test_good_rewrite_passes():
    good = ("Go to the bus stop Opposite Fengshan Primary School. Wait for bus number 45. Get on the bus. "
            "Tap your card. Count 5 stops. Get off at Tanah Merah Station Exit 2.")
    assert problems(good, BUS, 300) == []


def test_dropped_stop_name_fails():
    assert any("dropped" in p for p in problems("Wait for bus number 45. Count 5 stops. Get off.", BUS, 300))


def test_invented_number_fails():
    text = BUS["text"].replace("Exit 2", "Exit 2 near Block 731")
    assert any("731" in p for p in problems(text, BUS, 300))


def test_keep_is_case_insensitive_and_too_long_fails():
    assert problems("walk to [PLACE 1].", WALK, 300) == []
    assert any("longer" in p for p in problems("Walk to [place 1]. " * 30, WALK, 300))


# ----- the endpoint -----

@respx.mock
def test_uses_ai_text_that_passes_and_falls_back_per_step():
    good_walk = "Walk to [place 1]. It takes about 4 minutes."
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=answer((1, "Take bus 99."), (2, good_walk)))
    body = post(BUS, WALK).json()
    assert body["provider"] == "ollama" and body["model"] == "qwen2.5:1.5b"
    bus, walk = body["steps"]
    assert bus == {"stepIndex": 1, "easyRead": BUS["text"], "fallback": True}       # 99 was invented
    assert walk == {"stepIndex": 2, "easyRead": good_walk, "fallback": False}


@respx.mock
def test_step_the_ai_skipped_falls_back():
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=answer())
    assert post(WALK).json()["steps"][0]["fallback"] is True


@respx.mock
def test_misnumbered_answer_is_matched_by_position():
    good_walk = "Walk to [place 1]. It takes about 4 minutes."
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=answer((1, good_walk)))     # copied the example's index
    assert post({**WALK, "stepIndex": 0}).json()["steps"][0] == {"stepIndex": 0, "easyRead": good_walk,
                                                                 "fallback": False}


@respx.mock
def test_misnumbered_answer_still_checked_against_its_own_step():
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=answer((5, BUS["text"]), (6, "Walk to [place 1].")))
    bus, walk = post(BUS, WALK).json()["steps"]
    assert bus["fallback"] is False and walk["fallback"] is False
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=answer((5, "Walk to [place 1]."), (6, BUS["text"])))
    bus, walk = post(BUS, WALK).json()["steps"]
    assert bus["fallback"] is True and walk["fallback"] is True                     # swapped: both caught


@respx.mock
def test_ollama_down_answers_with_template_text():
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    r = post(BUS, WALK)
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "template" and body["model"] is None
    assert [s["easyRead"] for s in body["steps"]] == [BUS["text"], WALK["text"]]


@respx.mock
def test_ollama_answer_that_is_not_json_counts_as_down():
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(
        200, json={"message": {"role": "assistant", "content": "Sure! Here you go"}}))
    assert post(WALK).json()["provider"] == "template"


def test_bad_request_uses_contract_error_format():
    r = post({"stepIndex": 0})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


@respx.mock
def test_ready_needs_the_model_pulled():
    route = respx.get(f"{OLLAMA}/api/tags")
    with TestClient(create_app(s=SETTINGS)) as c:
        route.mock(return_value=httpx.Response(200, json={"models": [{"name": "llama3.2:1b"}]}))
        assert c.get("/ready").status_code == 503
        route.mock(return_value=httpx.Response(200, json={"models": [{"name": "qwen2.5:1.5b"}]}))
        assert c.get("/ready").status_code == 200
        assert c.get("/health").json() == {"status": "ok"}
