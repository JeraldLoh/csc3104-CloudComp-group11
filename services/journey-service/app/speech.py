"""Spoken instructions: each step's easy-read wording read aloud by Amazon Polly, for the traveller's phone.

    speech_key(text, voice, engine, rate) -> "media/speech/v1/<voice>-<engine>-<rate>/<hash>.mp3", the S3 key
    ssml(text, rate)                      -> the SSML Polly reads (slower, a pause between sentences)
    Speech(polly, store, ...).keys(texts) -> the S3 key of each text's MP3 (made if missing), or None

The route pack gives each step an `audioUrl` (contracts/examples/route-pack.json). The phone downloads it with
the photos before the trip, so the step is read aloud with no signal, in the same voice every time. Without one
(TTS_MODE=off, or Polly failed) the phone reads the text with its own voice instead (apps/traveller StepCard).

Files are named by what is said and how, so the same words are only ever made once, and changed words (the
caregiver edited the step) get a new file. Nothing is stored on the route itself.

Polly is only called by the service (IRSA role on EKS: polly:SynthesizeSpeech). LocalStack doesn't do Polly, so
locally TTS_MODE stays off unless real AWS credentials are given.
"""
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

log = logging.getLogger("journey")

VERSION = "v1"            # bump when ssml() changes how things are read, so old files aren't reused
MAX_CHARS = 1500          # the longest text Polly can read aloud (phone's stepCard only shows first 1500 chars)
PAUSE = '<break time="600ms"/>'

# "bus 190", "bus number 190", "bus 3A": Singapore bus numbers are said digit by digit ("one nine zero"),
# not as a number ("one hundred and ninety").
_BUS = re.compile(r"\b(bus(?:es)?(?: number)?\s+)(\d{1,3})([A-Z]?)\b", re.IGNORECASE)
# "bus stop 65009": a stop code is read out digit by digit too, as on the pole.
_STOP = re.compile(r"\b(stop(?: number)?\s+)(\d{5})\b", re.IGNORECASE)


class SpeechUnavailable(Exception):
    """Polly or S3 couldn't be reached or refused. The message is for the log."""


def speech_key(text: str, voice: str, engine: str, rate: str) -> str:
    digest = hashlib.sha256(f"{VERSION}\n{voice}\n{engine}\n{rate}\n{text}".encode("utf-8")).hexdigest()[:32]
    how = re.sub(r"[^a-z0-9]+", "-", f"{voice} {engine} {rate}".lower()).strip("-") or "default"   # joanna-neural-90
    return f"media/speech/{VERSION}/{how}/{digest}.mp3"


def sentences(text: str) -> list[str]:
    """The sentences of an easy-read text (one action each), as the apps show them as bullet points."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip(" •-\t") for p in parts if p.strip(" •-\t")]


def ssml(text: str, rate: str = "90%") -> str:
    """The text as SSML: a little slower than normal, a pause after each sentence, bus numbers and stop codes
    read digit by digit. Everything from the text is escaped, so its words can't become SSML tags."""
    said = []
    for s in sentences(text):
        s = escape(s)
        s = _BUS.sub(lambda m: f'{m.group(1)}<say-as interpret-as="characters">{m.group(2)}{m.group(3)}</say-as>', s)
        s = _STOP.sub(lambda m: f'{m.group(1)}<say-as interpret-as="digits">{m.group(2)}</say-as>', s)
        said.append(s if s[-1:] in ".!?" else s + ".")
    return f'<speak><prosody rate="{escape(rate)}">{PAUSE.join(said)}</prosody></speak>'


class Speech:
    def __init__(self, polly, store, voice: str = "Joanna", engine: str = "neural", rate: str = "90%",
                 workers: int = 4):
        """`polly`: a boto3 Polly client. `store`: the pack bucket (pack_store.S3PackStorage)."""
        self._polly, self._store = polly, store
        self.voice, self.engine, self.rate = voice, engine, rate
        self._workers = workers
        self._known: set[str] = set()          # keys already in S3: a pack built again only checks once

    @classmethod
    def from_settings(cls, s, store) -> "Speech":
        import boto3
        from botocore.config import Config
        cfg = Config(connect_timeout=s.tts_timeout_s, read_timeout=s.tts_timeout_s, retries={"max_attempts": 2})
        polly = boto3.client("polly", region_name=s.aws_region, endpoint_url=s.tts_endpoint_url, config=cfg)
        return cls(polly, store, s.tts_voice, s.tts_engine, s.tts_rate)

    def key_for(self, text: str) -> str:
        return speech_key(text, self.voice, self.engine, self.rate)

    def keys(self, texts: list[str | None]) -> list[str | None]:
        """The S3 key of the MP3 for each text, making the missing ones (in parallel). None for an empty text or
        one that couldn't be made: the phone then uses its own voice. Never raises."""
        out: list[str | None] = [None] * len(texts)
        todo: dict[str, list[int]] = {}
        for i, text in enumerate(texts):
            text = (text or "").strip()
            if not text or len(text) > MAX_CHARS:
                continue
            todo.setdefault(text, []).append(i)
        if not todo:
            return out
        with ThreadPoolExecutor(max_workers=min(self._workers, len(todo))) as pool:
            made = dict(zip(todo, pool.map(self._ensure, todo)))
        for text, where in todo.items():
            for i in where:
                out[i] = made[text]
        return out
    
    def _ensure(self, text: str) -> str | None:
        key = self.key_for(text)
        try:
            if key not in self._known and not self._store.exists(key):
                self._store.put(key, self._synthesize(text), content_type="audio/mpeg")
            self._known.add(key)
            return key
        except Exception as e:                  
            log.warning("spoken instruction unavailable: %s", type(e).__name__)
            return None

    def _synthesize(self, text: str) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            r = self._polly.synthesize_speech(Text=ssml(text, self.rate), TextType="ssml", OutputFormat="mp3",
                                              VoiceId=self.voice, Engine=self.engine)
            audio = r["AudioStream"].read()
        except ClientError as e:
            raise SpeechUnavailable(e.response.get("Error", {}).get("Code", "ClientError")) from e
        except BotoCoreError as e:
            raise SpeechUnavailable(type(e).__name__) from e
        if not audio:
            raise SpeechUnavailable("empty audio")
        return audio
