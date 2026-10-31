"""The Easy-read Service with Ollama mocked (respx): the AI text is used only when it passes the checks."""
import json

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.hallucination_check import problems
from app.main import create_app
from app.ollama_client import place

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
    assert bus == {"stepIndex": 1, "easyRead": BUS["text"], "fallback": True, "landmarks": []}   # 99 was invented
    assert walk == {"stepIndex": 2, "easyRead": good_walk, "fallback": False, "landmarks": []}


@respx.mock
def test_step_the_ai_skipped_falls_back():
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=answer())
    assert post(WALK).json()["steps"][0]["fallback"] is True


@respx.mock
def test_misnumbered_answer_is_matched_by_position():
    good_walk = "Walk to [place 1]. It takes about 4 minutes."
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=answer((1, good_walk)))     # copied the example's index
    assert post({**WALK, "stepIndex": 0}).json()["steps"][0] == {"stepIndex": 0, "easyRead": good_walk,
                                                                 "fallback": False, "landmarks": []}


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


# ----- landmarks -----

MALL = {"id": "L1", "name": "Tampines Mall", "kind": "mall", "metres": 20, "side": "left"}
BLOCK = {"id": "L2", "name": "Cpf Building", "kind": "building", "metres": 40, "side": "right"}
HAWKER = {"id": "L3", "name": "Bedok Hawker Centre", "kind": "hawker centre", "metres": 30, "side": None}
WALK_SPOTS = {**WALK, "spots": {"end": [MALL]}}
BUS_SPOTS = {**BUS, "spots": {"before_stop": [HAWKER, BLOCK], "end": [BLOCK]}}


def reply(content=None, calls=()) -> httpx.Response:
    """Ollama's /api/chat answer: JSON content, or tool calls given as (stepIndex, spot)."""
    message = {"role": "assistant", "content": json.dumps(content) if content is not None else "",
               "tool_calls": [{"function": {"name": "nearby", "arguments": {"stepIndex": i, "spot": spot}}}
                              for i, spot in calls] or None}
    return httpx.Response(200, json={"message": message, "done": True})


def rewritten(request: httpx.Request) -> httpx.Response:
    """The rewrite call answering with each step's own (landmark-placed) text."""
    question = json.loads(json.loads(request.content)["messages"][1]["content"])
    return reply({"steps": [{"stepIndex": s["stepIndex"], "easyRead": s["text"]} for s in question["steps"]]})


def sent(call) -> dict:
    return json.loads(call.request.content)


def test_place_puts_each_landmark_where_it_is_seen():
    bus = place(BUS, [("before_stop", HAWKER), ("end", BLOCK)])
    assert bus["text"] == ("At Opposite Fengshan Primary School, wait for bus number 45. Get on and tap your card. "
                           "Look out for Bedok Hawker Centre. Press the bell. Get off at Tanah Merah Station Exit 2 "
                           "after 5 stops. Look for Cpf Building on your right.")
    assert bus["keep"][-2:] == ["Bedok Hawker Centre", "Cpf Building"] and bus["landmarks"] == ["L3", "L2"]
    walk = place(WALK, [("start", MALL), ("middle", HAWKER)])
    assert walk["text"] == ("Look for Tampines Mall on your left. Walk to [place 1]. It takes about 4 minutes. "
                            "Walk past Bedok Hawker Centre.")
    assert place(WALK, []) == {**WALK, "landmarks": []}


def test_made_up_name_fails():
    step = place(WALK, [("end", MALL)])
    assert problems(step["text"], step, 300) == []
    text = step["text"].replace("Tampines Mall", "Changi Airport")
    assert problems(text, step, 300) == ["dropped ['Tampines Mall']", "names not in the step: ['airport', 'changi']"]


@respx.mock
def test_model_looks_up_chooses_and_the_landmarks_are_placed():
    chat = respx.post(f"{OLLAMA}/api/chat")
    chat.side_effect = [reply(calls=[(1, "before_stop"), (2, "end")]), reply(calls=[]),
                        reply({"step 1 before_stop": "Bedok Hawker Centre", "step 1 end": "none",
                               "step 2 end": "Tampines Mall"}), rewritten]
    bus, walk = post(BUS_SPOTS, WALK_SPOTS).json()["steps"]
    assert bus["landmarks"] == ["L3"] and not bus["fallback"]
    assert "Look out for Bedok Hawker Centre. Press the bell. Get off at" in bus["easyRead"]
    assert walk == {"stepIndex": 2, "easyRead": "Walk to [place 1]. It takes about 4 minutes. "
                    "Look for Tampines Mall on your left.", "fallback": False, "landmarks": ["L1"]}
    lookup, _, choose, rewrite = (sent(c) for c in chat.calls)
    assert lookup["tools"][0]["function"]["name"] == "nearby" and lookup["options"]["num_predict"] == 200
    tool_answers = [json.loads(m["content"]) for m in sent(chat.calls[1])["messages"] if m["role"] == "tool"]
    assert tool_answers == [[{k: b[k] for k in ("name", "kind", "metres", "side")} for b in (HAWKER, BLOCK)],
                            [{k: MALL[k] for k in ("name", "kind", "metres", "side")}]]
    # One key per spot, each allowing only that spot's names; "none" only where all are plain buildings.
    keys = choose["format"]["properties"]
    assert keys["step 1 before_stop"]["enum"] == ["Bedok Hawker Centre", "Cpf Building"]
    assert keys["step 1 end"]["enum"] == ["Cpf Building", "none"] and keys["step 2 end"]["enum"] == ["Tampines Mall"]
    assert "tools" not in rewrite and "Tampines Mall" in json.dumps(rewrite["messages"])
    # The model only ever gets names, kinds, sides and distances, never ids or coordinates.
    assert '"L1"' not in json.dumps([lookup, choose, rewrite])


@respx.mock
def test_spot_it_did_not_ask_about_is_still_offered():
    chat = respx.post(f"{OLLAMA}/api/chat")
    chat.side_effect = [reply(calls=[]), reply({"step 2 end": "Tampines Mall"}), rewritten]
    assert post(WALK_SPOTS).json()["steps"][0]["landmarks"] == ["L1"]


@respx.mock
def test_a_landmark_survives_a_failed_rewrite():
    chat = respx.post(f"{OLLAMA}/api/chat")
    chat.side_effect = [reply(calls=[]), reply({"step 2 end": "Tampines Mall"}),
                        answer((2, "Walk to [place 1]. Look for Changi Airport."))]
    walk = post(WALK_SPOTS).json()["steps"][0]
    assert walk == {"stepIndex": 2, "fallback": True, "landmarks": ["L1"],
                    "easyRead": "Walk to [place 1]. It takes about 4 minutes. Look for Tampines Mall on your left."}


@respx.mock
def test_bad_tool_calls_get_an_error():
    chat = respx.post(f"{OLLAMA}/api/chat")
    chat.side_effect = [reply(calls=[(9, "end"), (2, "middle")]), reply(calls=[]), reply({"step 2 end": "none"}),
                        rewritten]
    assert post(WALK_SPOTS).json()["steps"][0]["fallback"] is False
    errors = [json.loads(m["content"]) for m in sent(chat.calls[1])["messages"] if m["role"] == "tool"]
    assert len(errors) == 2 and all("error" in e for e in errors)


@respx.mock
def test_choosing_failing_still_rewords_without_landmarks():
    chat = respx.post(f"{OLLAMA}/api/chat")
    chat.side_effect = [reply(calls=[]), httpx.Response(200, json={"message": {"content": "not json"}}), rewritten]
    body = post(WALK_SPOTS).json()
    assert body["provider"] == "ollama" and body["steps"][0] == {"stepIndex": 2, "easyRead": WALK["text"],
                                                                 "fallback": False, "landmarks": []}


@respx.mock
def test_given_mode_makes_no_tool_calls():
    chat = respx.post(f"{OLLAMA}/api/chat")
    chat.side_effect = [reply({"step 2 end": "Tampines Mall"}), rewritten]
    with TestClient(create_app(s=Settings(ollama_url=OLLAMA, ollama_timeout_s=1, landmarks="given"))) as c:
        walk = c.post("/internal/easyread", json={"steps": [WALK_SPOTS]}).json()["steps"][0]
    assert walk["landmarks"] == ["L1"] and chat.call_count == 2 and "tools" not in sent(chat.calls[0])


@respx.mock
def test_steps_without_spots_keep_the_single_call():
    chat = respx.post(f"{OLLAMA}/api/chat").mock(return_value=answer((2, "Walk to [place 1].")))
    assert post(WALK).json()["steps"][0]["fallback"] is False
    assert chat.call_count == 1 and "tools" not in sent(chat.calls[0])
