"""The standard easy-read wording (easy_read.py): one short action per sentence, each starting with a verb."""
from app.easy_read import easy_read

BUS = {"mode": "bus", "line": "163", "stopCount": 3, "from": {"name": "Sengkang Int"}, "to": {"name": "Blk 126A"}}
TRAIN = {"mode": "train", "line": "NE", "stopCount": 1, "from": {"name": "Sengkang MRT Station"},
         "to": {"name": "Buangkok MRT Station"}}


def sentences(text: str) -> list[str]:
    return [s for s in text.split(". ") if s]


def test_bus_step_is_a_checklist():
    assert easy_read(BUS) == ("Go to Sengkang Interchange. Wait for bus number 163. Get on the bus. "
                              "Tap your card. Count 3 stops. Get off at Block 126A.")


def test_train_step_is_a_checklist():
    assert easy_read(TRAIN) == ("Go to Sengkang MRT Station. Take the North East Line train. Count 1 stop. "
                                "Get off at Buangkok MRT Station.")


def test_parts_that_are_not_known_are_left_out():
    text = easy_read({**BUS, "stopCount": None, "from": {}})
    assert text == "Wait for bus number 163. Get on the bus. Tap your card. Get off at Block 126A."
    assert easy_read({"mode": "walk", "to": {}, "expectedDurationS": 30}) == "Walk to your destination."


def test_every_sentence_is_short_and_starts_with_a_verb():
    walk = {"mode": "walk", "to": {"name": "Punggol Int"}, "expectedDurationS": 240}
    for step in (BUS, TRAIN, walk):
        for s in sentences(easy_read(step)):
            assert s.split()[0] in {"Go", "Wait", "Get", "Tap", "Count", "Take", "Walk", "It"}, s
            assert " and " not in s and "," not in s, s
