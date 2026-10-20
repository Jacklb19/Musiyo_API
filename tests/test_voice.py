"""Synthetic tests do not establish Spanish voice quality or headset compatibility."""
import asyncio
import base64
import io
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from test_guide import guide as guide
from test_guide import question

from app.content_operations import revoke
from app.guide_adapters import ProviderUnavailable
from app.guide_config import GuideSettings
from app.guide_service import GuideService
from app.main import create_app
from app.voice_adapters import (
    WhisperCppTranscriber,
    WindowsSapiSynthesizer,
    validate_wave,
)
from app.voice_config import VoiceSettings
from app.voice_routes import VoiceService, connected

CONSENT = {"X-Musiyo-Voice-Consent": "request-only-v1"}


def wav(seconds=0.2, rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0" * int(rate * seconds) * channels * 2)
    return output.getvalue()


class SyntheticVoice:
    async def transcribe(self, audio):
        return "Instrumento sintético"

    async def synthesize(self, text):
        return wav()


def client_for(guide, settings=None, adapter=None):
    service = VoiceService(guide[2], settings, transcriber=adapter, synthesizer=adapter)
    return TestClient(create_app(guide[1], guide_service=guide[2], voice_service=service))


def test_disabled_voice_is_honest_and_guide_v1_is_unchanged(guide):
    client = client_for(guide)
    capabilities = client.get("/api/v1/guide/voice-capabilities")
    assert capabilities.json()["transcription"] == capabilities.json()["synthesis"] == "disabled"
    result = client.post("/api/v1/guide/voice-answers", json=question().model_dump(), headers=CONSENT)
    assert result.status_code == 200
    body = result.json()
    assert body["response"]["status"] == "answered"
    assert body["speech_status"] == "disabled" and body["audio"] is None
    assert result.headers["cache-control"] == "private, no-store"
    original = client.post("/api/v1/guide/questions", json=question().model_dump())
    assert original.json() == body["response"]
    assert client.post("/api/v1/guide/transcriptions", content=wav(),
                       headers=CONSENT | {"Content-Type": "audio/wav"}).status_code == 503


def test_voice_requires_explicit_notice_acceptance_and_retains_no_request(guide):
    client = client_for(guide)
    response = client.post("/api/v1/guide/voice-answers", json=question().model_dump())
    assert response.status_code == 422
    assert "question" not in response.text and response.headers["cache-control"] == "private, no-store"


def test_normal_pipeline_with_synthetic_adapters_and_current_citations(guide):
    client = client_for(guide, VoiceSettings(transcription="whisper_cpp", synthesis="windows_sapi",
                                           windows_voice="Synthetic test voice"), SyntheticVoice())
    transcript = client.post("/api/v1/guide/transcriptions",
                             content=wav(), headers=CONSENT | {"Content-Type": "audio/wav"})
    assert transcript.status_code == 200 and transcript.json()["text"] == "Instrumento sintético"
    result = client.post("/api/v1/guide/voice-answers", json=question().model_dump(), headers=CONSENT).json()
    assert result["speech_status"] == "ready"
    validate_wave(base64.b64decode(result["audio"]["data_base64"]), 30, 5000000)
    assert result["response"]["citations"][0]["element_slug"] == "synthetic-import"


@pytest.mark.parametrize("audio", [b"not a wav", wav(31), wav(rate=8000), wav(channels=2), wav()[:-50]], ids=["not-wave", "duration", "rate", "channels", "truncated"])
def test_audio_content_and_duration_are_validated_without_calling_engine(guide, audio):
    class Uncalled(SyntheticVoice):
        async def transcribe(self, audio):
            pytest.fail("Invalid audio must never reach a provider")
    client = client_for(guide, VoiceSettings(transcription="whisper_cpp"), Uncalled())
    assert client.post("/api/v1/guide/transcriptions", content=audio,
                       headers=CONSENT | {"Content-Type": "audio/wav"}).status_code == 422


def test_audio_size_content_type_version_and_throttle(guide):
    client = client_for(guide, VoiceSettings(max_input_bytes=1000))
    path = "/api/v1/guide/transcriptions"
    assert client.post(path, content=b"x" * 1001, headers=CONSENT | {"Content-Type": "audio/wav"}).status_code == 413
    assert client.post(path, content=wav(), headers=CONSENT | {"Content-Type": "audio/webm"}).status_code == 422
    assert client.post(path + "?schema_version=2", content=wav(), headers=CONSENT).status_code == 409
    for _ in range(8):
        client.post("/api/v1/guide/voice-answers", json=question().model_dump(), headers=CONSENT)
    limited = client.post("/api/v1/guide/voice-answers", json=question().model_dump(), headers=CONSENT)
    assert limited.status_code == 429 and limited.headers["retry-after"] == "60"


@pytest.mark.parametrize("failure", ["timeout", "unavailable", "invalid"])
def test_synthesis_failure_preserves_grounded_text(guide, failure):
    class Failing(SyntheticVoice):
        async def synthesize(self, text):
            if failure == "invalid":
                return b"invalid wav"
            raise TimeoutError() if failure == "timeout" else ProviderUnavailable("private provider info")
    client = client_for(guide, VoiceSettings(synthesis="windows_sapi", windows_voice="Synthetic test voice"), Failing())
    response = client.post("/api/v1/guide/voice-answers", json=question().model_dump(), headers=CONSENT)
    assert response.json()["response"]["status"] == "answered"
    assert response.json()["speech_status"] == "unavailable" and response.json()["audio"] is None
    assert "private provider info" not in response.text


def test_revocation_during_synthesis_removes_both_text_and_audio(guide):
    class Revoking(SyntheticVoice):
        async def synthesize(self, text):
            revoke("synthetic-import", database_url=guide[0], reason="Synthetic revocation")
            return wav()
    client = client_for(guide, VoiceSettings(synthesis="windows_sapi", windows_voice="Synthetic test voice"), Revoking())
    result = client.post("/api/v1/guide/voice-answers", json=question().model_dump(), headers=CONSENT).json()
    assert result["response"]["status"] == "abstained"
    assert result["audio"] is None and result["speech_status"] == "not_applicable"


def test_provider_disabled_and_no_evidence_never_synthesize(guide):
    class Uncalled(SyntheticVoice):
        async def synthesize(self, text):
            pytest.fail("Unanswered questions must never be synthesized")
    service = VoiceService(guide[2], VoiceSettings(synthesis="windows_sapi", windows_voice="Test"), synthesizer=Uncalled())
    assert asyncio.run(service.answer(question("Astronautas galácticos"))).speech_status == "not_applicable"
    disabled = GuideService(guide[1], settings=GuideSettings(generation="disabled"))
    service.guide = disabled
    assert asyncio.run(service.answer(question())).response.status == "provider_unavailable"


def test_disconnect_cancels_local_engine_task():
    cancelled = []
    class Disconnected:
        async def is_disconnected(self):
            await asyncio.sleep(0)
            return True
    async def engine():
        try:
            await asyncio.sleep(100)
        finally:
            cancelled.append(True)
    with pytest.raises(Exception, match="499"):
        asyncio.run(connected(Disconnected(), engine(), 5))
    assert cancelled == [True]


@pytest.mark.parametrize("patch", [{"whisper_url": "https://external.invalid:80"},
                                   {"whisper_url": "http://127.0.0.1:80/redirect"},
                                   {"synthesis": "windows_sapi"}, {"engine_timeout_seconds": 21},
                                   {"max_input_seconds": 31}, {"retain_audio": True}])
def test_config_refuses_remote_or_unbounded_selection(patch):
    with pytest.raises(ValidationError):
        VoiceSettings(**patch)


def test_whisper_protocol_has_no_proxy_redirect_or_unbounded_output(monkeypatch):
    real_client = httpx.AsyncClient
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:8189/inference"
        assert b'name="language"' in request.content and b"question.wav" in request.content
        return httpx.Response(200, json={"text": " Pregunta sintética "})
    def selected_client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", selected_client)
    assert asyncio.run(WhisperCppTranscriber(VoiceSettings()).transcribe(wav())) == "Pregunta sintética"


def test_windows_adapter_refuses_missing_spanish_voice():
    # A real local failure probe: there is no selected installed Spanish voice.
    with pytest.raises(ProviderUnavailable):
        asyncio.run(WindowsSapiSynthesizer(VoiceSettings(synthesis="windows_sapi",
                    windows_voice="Nonexistent synthetic Spanish voice")).synthesize("Prueba técnica."))


def test_exported_voice_fixtures_match_canonical_models():
    import json
    root = Path(__file__).resolve().parents[1] / "contracts"
    for schema, example in [("guide-voice-answer", "guide_voice_disabled"),
                            ("guide-transcription", "guide_transcription"),
                            ("voice-capabilities", "voice_capabilities_disabled")]:
        Draft202012Validator(json.loads((root / (schema + ".v1.schema.json")).read_text())).validate(
            json.loads((root / "examples" / (example + ".json")).read_text()))


def test_pcm_wrapping_rejects_empty_or_odd_output():
    from app.voice_adapters import pcm_wave
    validate_wave(pcm_wave(b"\0\0" * 1600), 30, 5000000)
    for value in [b"", b"\0" * 3]:
        with pytest.raises(ValueError):
            pcm_wave(value)


def installed_windows_voice(name):
    import os
    import subprocess
    if os.name != "nt":
        return False
    script = ("Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
              ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=30)
    return name in result.stdout.splitlines()


@pytest.mark.skipif(not installed_windows_voice("Microsoft Helena Desktop"),
                    reason="Generic Windows Spanish voice is not installed")
def test_real_local_spanish_synthesis_through_voice_endpoint(guide):
    # Real engine, synthetic catalogue: proves the local path, not voice quality or Quest support.
    client = client_for(guide, VoiceSettings(synthesis="windows_sapi", windows_voice="Microsoft Helena Desktop"))
    result = client.post("/api/v1/guide/voice-answers", json=question().model_dump(), headers=CONSENT).json()
    assert result["speech_status"] == "ready"
    audio = base64.b64decode(result["audio"]["data_base64"])
    validate_wave(audio, 90, 3000000, input_audio=False)
    with wave.open(io.BytesIO(audio)) as sound:
        assert sound.getnframes() > 16000


def installed_windows_recognizer(culture):
    import os
    import subprocess
    if os.name != "nt":
        return False
    script = ("Add-Type -AssemblyName System.Speech; [System.Speech.Recognition.SpeechRecognitionEngine]"
              "::InstalledRecognizers() | ForEach-Object { $_.Culture.Name }")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=30)
    return culture in result.stdout.splitlines()


LOCAL_WINDOWS_VOICE = pytest.mark.skipif(
    not (installed_windows_voice("Microsoft Helena Desktop") and installed_windows_recognizer("es-ES")),
    reason="Generic Windows Spanish voice or recognizer is not installed")


@LOCAL_WINDOWS_VOICE
def test_real_local_spoken_question_round_trip(guide):
    # Synthetic speech only: a TTS-to-STT loop is a technical match, not human speech accuracy.
    settings = VoiceSettings(transcription="windows_sapi", synthesis="windows_sapi",
                             windows_voice="Microsoft Helena Desktop")
    spoken = asyncio.run(WindowsSapiSynthesizer(settings).synthesize("¿Qué instrumento produce sonido?"))
    client = client_for(guide, settings)
    transcript = client.post("/api/v1/guide/transcriptions", content=spoken,
                             headers=CONSENT | {"Content-Type": "audio/wav"})
    assert transcript.status_code == 200
    assert "instrumento" in transcript.json()["text"].lower()
    result = client.post("/api/v1/guide/voice-answers", headers=CONSENT, json={
        "schema_version": 1, "question": transcript.json()["text"], "element_slug": None}).json()
    assert result["response"]["status"] == "answered" and result["speech_status"] == "ready"


@LOCAL_WINDOWS_VOICE
def test_real_recognizer_refuses_silence_and_missing_culture(guide):
    from app.voice_adapters import WindowsSapiTranscriber
    with pytest.raises(ProviderUnavailable):
        asyncio.run(WindowsSapiTranscriber(VoiceSettings(transcription="windows_sapi")).transcribe(wav(1)))
    with pytest.raises(ProviderUnavailable):
        asyncio.run(WindowsSapiTranscriber(VoiceSettings(transcription="windows_sapi",
                                                         windows_recognizer_culture="es-AR")).transcribe(wav(1)))


def test_recognition_vocabulary_uses_only_currently_published_titles(guide):
    from app.voice_adapters import vocabulary_terms
    from app.voice_routes import catalogue_vocabulary
    assert sorted(vocabulary_terms(["¿Máscara de Sanjuán: silbido?", "  El   bombo "])) == sorted(
        ["¿Máscara de Sanjuán: silbido?", "El bombo", "Máscara", "Sanjuán", "silbido", "bombo"])
    assert len(vocabulary_terms(f"Título sintético {index}" for index in range(500))) == 200
    assert "Ficha sintética de instrumentos" in catalogue_vocabulary(guide[1])
    revoke("synthetic-import", database_url=guide[0], reason="Synthetic revocation")
    assert catalogue_vocabulary(guide[1]) == []


def test_transcriber_sends_frames_and_current_vocabulary_on_stdin(monkeypatch):
    import json

    from app import voice_adapters
    sent = {}

    async def script(name, payload, timeout, limit):
        sent.update(name=name, payload=json.loads(payload))
        return "Qué es el bombo".encode("utf-8")

    async def vocabulary():
        return ["El bombo"]
    monkeypatch.setattr(voice_adapters, "run_windows_script", script)
    settings = VoiceSettings(transcription="windows_sapi")
    assert asyncio.run(voice_adapters.WindowsSapiTranscriber(settings, vocabulary).transcribe(wav())) == "Qué es el bombo"
    assert sent["name"] == "recognize-local.ps1" and sent["payload"]["vocabulary"] == ["El bombo"]
    assert "qué es" in sent["payload"]["frames"] and sent["payload"]["culture"] == "es-ES"
    validate_wave(base64.b64decode(sent["payload"]["audio"]), 30, 5000000)
