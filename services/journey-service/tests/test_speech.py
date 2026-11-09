"""Spoken instructions (app/speech.py): Polly MP3s of each step's easy-read wording, linked from the route pack."""
import io

import respx

import fakeredis
from botocore.exceptions import ClientError

from app.journey_client import JourneyClient
from app.speech import Speech, sentences, speech_key, ssml
from helpers import NOW, SETTINGS, route_data
from test_streetview import FakeBucket

MP3 = b"ID3" + b"\x00" * 50


class FakePolly:
    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def synthesize_speech(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "SynthesizeSpeech")
        return {"AudioStream": io.BytesIO(MP3)}


def pack_client(polly=None, bucket=None):
    bucket = bucket if bucket is not None else FakeBucket()
    speech = Speech(polly, bucket) if polly is not None else None
    j = JourneyClient(SETTINGS, redis_client=fakeredis.FakeRedis(), sleep=lambda _: None, clock=lambda: NOW,
                      pack_store=bucket, speech=speech)
    return j, bucket


def build(j, **route_over):
    route = j.approve_route(j.create_route(route_data(**route_over))["routeId"])
    return j.build_pack(j.create_trip(route["routeId"], "2026-10-22")["tripId"])


# ---- SSML ----------------------------------------------------------------------------

def test_ssml_reads_slower_with_a_pause_between_sentences():
    out = ssml("Walk to the bus stop. Wait for the bus", "90%")
    assert out.startswith('<speak><prosody rate="90%">') and out.endswith("</prosody></speak>")
    assert 'Walk to the bus stop.<break time="600ms"/>Wait for the bus.' in out


def test_bus_numbers_and_stop_codes_are_read_digit_by_digit():
    out = ssml("Wait for bus number 190. Take bus 3A. Find bus stop 65009.")
    assert 'bus number <say-as interpret-as="characters">190</say-as>' in out
    assert 'bus <say-as interpret-as="characters">3A</say-as>' in out
    assert 'stop <say-as interpret-as="digits">65009</say-as>' in out


def test_text_cannot_inject_ssml():
    out = ssml('Go to <break time="10s"/> & "Block 5"')
    assert "<break time=\"10s\"/>" not in out and "&lt;break" in out and "&amp;" in out


def test_bullets_and_lines_become_sentences():
    assert sentences("• Walk to the stop.\n• Wait for bus 190\n\n- Get on.") == ["Walk to the stop.", "Wait for bus 190", "Get on."]


def test_key_changes_with_the_words_and_the_voice():
    a = speech_key("Walk.", "Joanna", "neural", "90%")
    assert a.startswith("media/speech/v1/joanna-neural-90/") and a.endswith(".mp3")
    assert a == speech_key("Walk.", "Joanna", "neural", "90%")
    assert a != speech_key("Walk!", "Joanna", "neural", "90%") != speech_key("Walk.", "Amy", "neural", "90%")


# ---- Speech ----------------------------------------------------------------------------

def test_each_text_is_made_once_and_stored_as_mp3():
    polly, bucket = FakePolly(), FakeBucket()
    speech = Speech(polly, bucket)
    keys = speech.keys(["Walk.", None, "Walk.", "Get on."])
    assert keys[1] is None and keys[0] == keys[2] != keys[3]
    assert len(polly.calls) == 2                                       # the same words: one file
    assert bucket.objects[keys[0]] == (MP3, "audio/mpeg")
    call = polly.calls[0]
    assert (call["TextType"], call["OutputFormat"], call["VoiceId"], call["Engine"]) == ("ssml", "mp3", "Joanna", "neural")
    speech.keys(["Walk."])
    Speech(polly, bucket).keys(["Get on."])                            # another pod: already in S3
    assert len(polly.calls) == 2


def test_polly_failure_gives_no_audio_rather_than_an_error():
    speech = Speech(FakePolly(fail=True), FakeBucket())
    assert speech.keys(["Walk."]) == [None]


# ---- the route pack ----------------------------------------------------------------------

def test_pack_links_each_step_to_its_spoken_wording():
    polly = FakePolly()
    j, bucket = pack_client(polly)
    pack = build(j)
    for step in pack["steps"]:
        key = speech_key(step["easyRead"], "Joanna", "neural", "90%")
        assert step["audioUrl"] == f"https://bucket.test/{key}?ttl={SETTINGS.photo_url_ttl_s}"
        assert key in bucket.objects
    said = [c["Text"] for c in polly.calls]
    assert any('<say-as interpret-as="characters">190</say-as>' in t for t in said)   # the bus step


def test_pack_without_speech_has_no_audio():
    j, _ = pack_client(polly=None)
    assert [s["audioUrl"] for s in build(j)["steps"]] == [None, None]


def test_a_steps_own_audio_wins_and_is_a_link_not_a_key():
    polly = FakePolly()
    j, bucket = pack_client(polly)
    data = route_data()
    data["steps"][0]["audioKey"] = "media/uploads/R-1/step0.mp3"
    pack = build(j, steps=data["steps"])
    assert pack["steps"][0]["audioUrl"].startswith("https://bucket.test/media/uploads/R-1/step0.mp3")
    assert pack["steps"][1]["audioUrl"].startswith("https://bucket.test/media/speech/")
    assert len(polly.calls) == 1                                        # only the step without its own audio


def test_pack_still_builds_when_polly_is_down():
    j, _ = pack_client(FakePolly(fail=True))
    pack = build(j)
    assert [s["audioUrl"] for s in pack["steps"]] == [None, None] and pack["steps"][0]["easyRead"]


# ---- made at a single caregiver action, not when the phone fetches its pack -------------------------------------

def test_approving_a_route_makes_its_audio_so_the_pack_calls_no_polly():
    polly = FakePolly()
    j, bucket = pack_client(polly)
    route = j.create_route(route_data())
    assert polly.calls == []                                            # a draft: nothing yet
    j.approve_route(route["routeId"])
    assert len(polly.calls) == 2 and sum(k.startswith("media/speech/") for k in bucket.objects) == 2
    j.approve_route(route["routeId"])                                   # again: already approved, nothing new
    pack = j.build_pack(j.create_trip(route["routeId"], "2026-10-22")["tripId"])
    assert len(polly.calls) == 2 and all(s["audioUrl"] for s in pack["steps"])


def test_polly_down_at_approval_still_approves_and_the_pack_makes_it_later():
    polly = FakePolly(fail=True)
    j, _ = pack_client(polly)
    route = j.approve_route(j.create_route(route_data())["routeId"])
    assert route["approved"]
    polly.fail = False                                                  # back up when the phone asks
    pack = j.build_pack(j.create_trip(route["routeId"], "2026-10-22")["tripId"])
    assert all(s["audioUrl"] for s in pack["steps"])


@respx.mock
def test_sending_a_changed_plan_makes_the_new_steps_audio_before_the_phone_asks():
    from datetime import timedelta
    from app.auth import Caller
    from test_bridge import Trip
    t = Trip()
    polly, bucket = FakePolly(), FakeBucket()
    t.j._packs, t.j._speech = bucket, Speech(polly, bucket)
    t.j.watch_bridges(NOW)
    assert polly.calls == []                                            # only proposed
    t.j.send_bridge(t.id, Caller("G-003", "caregiver"))
    new_steps = t.get()["override"]["steps"]
    for step in new_steps:
        assert speech_key(step["easyRead"], "Joanna", "neural", "90%") in bucket.objects
    made = len(polly.calls)
    pack = t.pack()
    assert len(polly.calls) - made == 1                                 # only the walk before, approved without Polly here
    assert all(s["audioUrl"] for s in pack["steps"])
