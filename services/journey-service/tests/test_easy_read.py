"""The standard easy-read wording (easy_read.py): one short action per sentence, each starting with a verb, with
what heads off the usual mistakes (the wrong side of the road, the wrong direction, missing the stop)."""
import re

from app.easy_read import MAX_CHARS, easy_read, ordinal, route_wording

STOPS = [{"name": "Sengkang Int"}, {"name": "Blk 268C"}, {"name": "Blk 125"}, {"name": "Blk 126A"}]
BUS = {"mode": "bus", "line": "163", "stopCount": 3, "expectedDurationS": 540, "stops": STOPS,
       "from": {"name": "Sengkang Int", "code": "67009"}, "to": {"name": "Blk 126A", "code": "67259"},
       "towards": "Bef Punggol Rd", "every": [8, 12]}
TRAIN = {"mode": "train", "line": "NE", "stopCount": 11, "from": {"name": "Punggol MRT Station", "code": "NE17"},
         "to": {"name": "Dhoby Ghaut MRT Station", "code": "NE6"}, "stops": [{"code": "NE17"}, {"code": "NE6"}]}


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=\.)\s+", text) if s]


def test_bus_step():
    assert easy_read(BUS) == (
        "Find bus stop 67009 at Sengkang Interchange. Check the number on the pole. "
        "Raise your hand for bus 163 to Before Punggol Road. It comes every 8 to 12 minutes. Tap your card to get on. "
        "The ride takes about 9 minutes. After Block 125, press the bell. Get off at Block 126A. "
        "Tap your card to get off.")


def test_bus_one_stop_and_unknown_parts():
    one = easy_read({**BUS, "stopCount": 1, "stops": STOPS[:1] + STOPS[-1:]})
    assert "Press the bell when the bus moves off. Get off at the next stop, Block 126A." in one
    # Older routes: no stop code, stops, direction or frequency. The stop count stands in for the bell cue.
    bare = easy_read({"mode": "bus", "line": "163", "stopCount": 3, "from": {"name": "Sengkang Int"},
                      "to": {"name": "Blk 126A"}})
    assert bare == ("Go to Sengkang Interchange. Raise your hand for bus 163. Tap your card to get on. "
                    "Get off at Block 126A. It is the 3rd stop. Tap your card to get off.")


def test_long_bus_wording_leaves_out_how_often_then_how_long():
    long = {**BUS, "from": {**BUS["from"], "name": "Opp Sengkang General Hospital"},
            "to": {**BUS["to"], "name": "Aft Sengkang Community Hub"}}
    text = easy_read(long)
    assert len(text) <= MAX_CHARS
    assert "It comes every" not in text and "The ride takes" in text
    assert "Before Punggol Road" in text and "After Block 125, press the bell." in text      # never the safety parts


def test_train_step_says_which_way_and_which_signs():
    assert easy_read(TRAIN) == ("Tap your card at the gantry. Follow the purple signs for the North East Line. "
                                "Take the train going to HarbourFront. Get off at Dhoby Ghaut MRT Station. "
                                "It is the 11th stop.")
    up = {**TRAIN, "stops": [{"code": "NE6"}, {"code": "NE17"}]}
    assert "Take the train going to Punggol Coast." in easy_read(up)
    # No two simple ends (the Circle Line, LRT loops): the station it stops at next tells the platform.
    circle = {**TRAIN, "line": "CC", "from": {"name": "Serangoon MRT Station", "code": "CC13"},
              "stops": [{"code": "CC13", "name": "Serangoon"}, {"code": "CC14", "name": "Lorong Chuan"},
                        {"code": "CC15", "name": "Bishan"}]}
    assert "Follow the yellow signs for the Circle Line. Take the train that stops next at Lorong Chuan." in easy_read(circle)
    lrt = {**TRAIN, "line": "SW", "stops": [{"code": "SW1"}, {"code": "SW4"}]}
    assert "Get on the train." in easy_read(lrt) and "going to" not in easy_read(lrt)     # one stop, no names: plain


def test_route_wording_knows_the_steps_around():
    walk = {"mode": "walk", "from": {"name": "Home"}, "to": {"name": "Punggol MRT Station", "code": "NE17"},
            "expectedDurationS": 240}
    change = {"mode": "walk", "from": {"name": "Dhoby Ghaut", "code": "NE6"}, "to": {"name": "Dhoby Ghaut", "code": "NS24"},
              "expectedDurationS": 240}
    second = {"mode": "train", "line": "NS", "stopCount": 2, "from": {"name": "Dhoby Ghaut", "code": "NS24"},
              "to": {"name": "Orchard MRT Station", "code": "NS22"}, "stops": [{"code": "NS24"}, {"code": "NS22"}]}
    last = {"mode": "walk", "from": {"name": "Orchard MRT Station", "code": "NS22"}, "to": {}, "expectedDurationS": 180}
    wording = route_wording([walk, TRAIN, change, second, last], "ION Orchard")
    assert wording[0] == "Walk to Punggol MRT Station. It takes about 4 minutes."
    assert wording[2] == "Change to the red North South Line. Follow the red signs. It takes about 4 minutes."
    assert not wording[3].startswith("Tap your card")                    # changed lines inside: no gantry
    assert wording[3].startswith("Follow the red signs for the North South Line. Take the train going to Jurong East.")
    assert wording[4] == "Walk to ION Orchard. It takes about 3 minutes. You have arrived."


def test_walk_to_a_bus_stop_says_so():
    walk = {"mode": "walk", "to": {"name": "Blk 119D", "code": "65009"}, "expectedDurationS": 180}
    assert easy_read(walk) == "Walk to the bus stop at Block 119D. It takes about 3 minutes."
    assert easy_read({"mode": "walk", "to": {}, "expectedDurationS": 30}) == "Walk to your destination."


def test_ordinal():
    assert [ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st"]


def test_every_sentence_is_short_and_starts_with_a_verb():
    walk = {"mode": "walk", "to": {"name": "Punggol Int", "code": "65009"}, "expectedDurationS": 240}
    starts = {"Find", "Check", "Raise", "Tap", "Get", "Press", "Follow", "Take", "Change", "Walk", "Go", "After",
              "It", "The", "You"}
    for step in (BUS, TRAIN, walk):
        for s in sentences(easy_read(step)):
            assert s.split()[0] in starts, s
            assert " and " not in s, s
            # One comma at most, only to name a place: "After Block 125, press the bell."
            assert s.count(",") <= 1 and (s.count(",") == 0 or s.startswith(("After", "Get off at the next stop"))), s


def test_lta_short_forms_are_spelled_out():
    from app.easy_read import plain
    assert plain("Blk 14 Mkt/Fc") == "Block 14 Market and Food Centre"
    assert plain("Opp Bedok Nth Ave 3") == "Opposite Bedok North Avenue 3"


def test_saving_names_the_destination_in_the_planners_own_wording():
    from app.easy_read import with_easy_read
    last = {"mode": "walk", "from": {"name": "Orchard MRT Station", "code": "NS22"}, "to": {}, "expectedDurationS": 180}
    planned = route_wording([TRAIN, last])                             # what the plan had: no destination name
    data = {"destination": {"name": "ION Orchard"}, "steps": [{**TRAIN, "easyRead": planned[0]}, {**last, "easyRead": planned[1]}]}
    assert with_easy_read(data)["steps"][1]["easyRead"] == "Walk to ION Orchard. It takes about 3 minutes. You have arrived."
    edited = {**data, "steps": [data["steps"][0], {**data["steps"][1], "easyRead": "Walk to the big mall."}]}
    assert with_easy_read(edited)["steps"][1]["easyRead"] == "Walk to the big mall."        # the caregiver's stays
