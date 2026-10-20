import hashlib
from pathlib import Path

import pytest
from test_guide import guide as guide

from app.guide_adapters import ProviderUnavailable
from app.guide_config import GuideSettings
from app.voice_config import VoiceSettings
from scripts.evaluate_guide import EvaluationCase
from scripts.evaluate_voice import evaluate, word_accuracy

VOICE = VoiceSettings(transcription="windows_sapi", synthesis="windows_sapi", windows_voice="Synthetic test voice")


class Echo:
    """Synthetic engines: 'audio' is the encoded text, transcription may drop accents or fail."""

    def __init__(self, transform=lambda text: text):
        self.transform = transform

    async def synthesize(self, text):
        return text.encode("utf-8")

    async def transcribe(self, audio):
        result = self.transform(audio.decode("utf-8"))
        if result is None:
            raise ProviderUnavailable("synthetic failure")
        return result


def cases():
    return [EvaluationCase(id="pertinent", category="pertinent", expected_status="answered", expected_citations=["synthetic-import"],
                           request=dict(schema_version=1, question="¿Qué instrumento produce sonido?")),
            EvaluationCase(id="outside", category="out-of-evidence", expected_status="abstained",
                           request=dict(schema_version=1, question="¿Cuál es la temperatura de Marte?"))]


def test_word_accuracy_ignores_case_accents_and_punctuation():
    assert word_accuracy("¿Qué es el Matachín?", "que es el matachin") == 1
    assert word_accuracy("¿Qué es el Matachín?", "Kessel matachines") == 0
    assert word_accuracy("uno dos", "") == 0


def test_spoken_evaluation_is_read_only_and_stores_no_text(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    report = evaluate(database, GuideSettings(), VOICE, cases(), synthesizer=Echo(), transcriber=Echo())
    assert report["database_unchanged"] and hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert report["typed_matched"] == report["spoken_matched"] == report["transcribed"] == 2
    assert report["mean_word_accuracy"] == 1
    for item in report["results"]:
        assert not {"question", "answer", "transcript", "request"} & set(item)


def test_transcription_errors_are_reported_separately_from_guide_quality(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    report = evaluate(database, GuideSettings(), VOICE, cases(), synthesizer=Echo(), transcriber=Echo(lambda text: None))
    assert report["typed_matched"] == 2 and report["spoken_matched"] == 0 and report["transcribed"] == 0
    assert {item["spoken_status"] for item in report["results"]} == {"not_transcribed"}


def test_spoken_evaluation_requires_explicit_local_engines(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    with pytest.raises(ValueError):
        evaluate(database, GuideSettings(), VoiceSettings(), cases())
    with pytest.raises(ValueError):
        evaluate(database, GuideSettings(generation="disabled"), VOICE, cases())
