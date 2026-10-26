"""Contract tests for Groq/ElevenLabs/Piper adapters with simulated HTTP and processes.

They prove request shape, key handling, validation and fallbacks. They do not prove that the
real providers answer, their Spanish quality, quotas or availability.
"""
import asyncio
import io
import json
import sys
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_guide import guide as guide
from test_guide import question

from app import remote_http, voice_adapters
from app.guide_adapters import LocalExtractiveGeneration, ProviderUnavailable
from app.guide_clarification import ambiguous_records, clarification_text
from app.guide_config import GuideSettings
from app.guide_groq import FallbackGeneration, GroqGeneration
from app.guide_service import GuideService
from app.main import create_app
from app.voice_adapters import (
    ChainSynthesizer,
    ChainTranscriber,
    ElevenLabsSynthesizer,
    GroqTranscriber,
    PiperSynthesizer,
    validate_wave,
)
from app.voice_config import VoiceSettings
from app.voice_routes import VoiceService

CONSENT = {"X-Musiyo-Voice-Consent": "request-only-v1"}


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "provider.key"
    path.write_text("synthetic-test-key\n", encoding="utf-8")
    return str(path)


def wav(seconds=0.5, rate=16000):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * int(rate * seconds))
    return output.getvalue()


def groq_settings(key, **extra):
    return GuideSettings(generation="groq", groq_model="openai/gpt-oss-120b", groq_api_key_file=key, **extra)


def test_groq_generation_sends_only_question_and_evidence_and_cites_fragments(guide, key):
    calls = []

    def fake_post(url, payload, headers, timeout, limit):
        calls.append((url, payload, headers))
        content = json.dumps({"answer": "Respuesta sintética redactada.", "fragment_ids": ["E1"], "sufficient": True})
        return {"choices": [{"message": {"content": content}}]}

    service = GuideService(guide[1], groq_settings(key), generation=GroqGeneration(groq_settings(key), post=fake_post))
    result = service.ask(question())
    assert result.status == "answered" and result.answer.startswith("Respuesta sintética redactada")
    assert [item.element_slug for item in result.citations] == ["synthetic-import"]
    url, payload, headers = calls[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions" and headers["Authorization"] == "Bearer synthetic-test-key"
    assert payload["response_format"]["json_schema"]["strict"] is True
    sent = json.loads(payload["messages"][1]["content"])
    assert set(sent) == {"question", "evidence"} and sent["evidence"][0]["fragment_id"] == "E1"


@pytest.mark.parametrize("failure", ["schema", "unavailable"])
def test_provider_failure_falls_back_to_the_local_extractive_guide(guide, key, failure):
    def fake_post(*args):
        if failure == "unavailable":
            raise ProviderUnavailable("quota")
        return {"choices": [{"message": {"content": "not json"}}]}

    generation = FallbackGeneration(GroqGeneration(groq_settings(key), post=fake_post), LocalExtractiveGeneration())
    result = GuideService(guide[1], groq_settings(key), generation=generation).ask(question())
    assert result.status == "answered" and "«" in result.answer


def test_provider_abstention_is_respected_and_invented_fragments_refused(guide, key):
    for content in [{"answer": "", "fragment_ids": [], "sufficient": False},
                    {"answer": "Inventado", "fragment_ids": ["E9"], "sufficient": True}]:
        def fake_post(*args, content=content):
            return {"choices": [{"message": {"content": json.dumps(content)}}]}
        generation = FallbackGeneration(GroqGeneration(groq_settings(key), post=fake_post), LocalExtractiveGeneration())
        assert GuideService(guide[1], groq_settings(key), generation=generation).ask(question()).status == "abstained"


def test_external_settings_and_hosts_are_explicit(tmp_path, key):
    with pytest.raises(ValidationError):
        GuideSettings(generation="groq", groq_model="openai/gpt-oss-120b")
    with pytest.raises(ProviderUnavailable):
        remote_http.post("http://api.groq.com/x", b"", {}, 1, 10)
    with pytest.raises(ProviderUnavailable):
        remote_http.post("https://example.com/x", b"", {}, 1, 10)
    inside = Path(remote_http.__file__).resolve().parents[1] / "README.md"
    with pytest.raises(ProviderUnavailable):
        remote_http.read_secret(str(inside))
    with pytest.raises(ProviderUnavailable):
        remote_http.read_secret(str(tmp_path / "missing.key"))
    assert remote_http.read_secret(key) == "synthetic-test-key"


def test_ambiguous_generic_term_lists_current_titles_instead_of_choosing_one():
    titles = {"matachin": "El Matachín", "matachin-mask": "Máscara del Matachín",
              "a": "Máscara de Sanjuán: figura animal", "b": "Máscara de Sanjuán: silbido", "bombo": "El bombo"}
    assert ambiguous_records("¿Qué es la máscara?", titles) == ["a", "b", "matachin-mask"]
    assert ambiguous_records("¿De qué están hechas las máscaras?", titles) == ["a", "b", "matachin-mask"]
    assert ambiguous_records("¿Qué es la máscara de Sanjuán?", titles) == ["a", "b"]
    for named in ["¿Qué es la Máscara del Matachín?", "¿Quiénes son los matachines?", "¿Qué es el bombo?",
                  "¿Qué es la máscara de Sanjuán con silbido?", "¿Qué instrumento suena?"]:
        assert ambiguous_records(named, titles) == []
    assert clarification_text(["a", "b"], titles) == (
        "Hay 2 fichas que coinciden con tu pregunta: Máscara de Sanjuán: figura animal y "
        "Máscara de Sanjuán: silbido. ¿Sobre cuál quieres saber?")


def test_groq_transcription_and_elevenlabs_synthesis_shapes(monkeypatch, key):
    sent = {}

    def fake_multipart(url, fields, file, headers, timeout, limit):
        sent.update(url=url, fields=fields, name=file[0], auth=headers["Authorization"])
        return {"text": "  ¿Qué es   el bombo? "}

    def fake_post(url, body, headers, timeout, limit):
        sent.update(tts_url=url, tts_body=json.loads(body), tts_key=headers["xi-api-key"])
        return b"\x01\x00" * 8000

    monkeypatch.setattr(voice_adapters, "post_multipart", fake_multipart)
    monkeypatch.setattr(voice_adapters, "post", fake_post)
    settings = VoiceSettings(transcription="groq", synthesis="elevenlabs", groq_api_key_file=key,
                             elevenlabs_api_key_file=key, elevenlabs_voice_id="SyntheticVoice01")
    assert asyncio.run(GroqTranscriber(settings).transcribe(wav())) == "¿Qué es el bombo?"
    assert sent["fields"]["model"] == "whisper-large-v3-turbo" and sent["fields"]["language"] == "es"
    assert sent["auth"] == "Bearer synthetic-test-key"
    audio = asyncio.run(ElevenLabsSynthesizer(settings).synthesize("Respuesta sintética."))
    validate_wave(audio, 90, 3000000, input_audio=False)
    assert sent["tts_url"].endswith("/SyntheticVoice01?output_format=pcm_16000")
    assert sent["tts_body"] == {"text": "Respuesta sintética.", "model_id": "eleven_multilingual_v2"}


def test_piper_protocol_reads_model_rate_and_wraps_raw_output(tmp_path, monkeypatch):
    model = tmp_path / "voz.onnx"
    model.write_bytes(b"synthetic")
    (tmp_path / "voz.onnx.json").write_text(json.dumps({"audio": {"sample_rate": 22050}}), encoding="utf-8")
    fake = tmp_path / "fake_piper.py"
    fake.write_text("import sys\nsys.stdin.readline()\nsys.stdout.buffer.write(b'\\x00\\x01' * 22050)\n", encoding="utf-8")
    monkeypatch.setattr(PiperSynthesizer, "command", lambda self, executable, model: [sys.executable, str(fake)])
    settings = VoiceSettings(synthesis="piper", piper_executable=sys.executable, piper_model=str(model))
    audio = asyncio.run(PiperSynthesizer(settings).synthesize("Prueba sintética."))
    with wave.open(io.BytesIO(audio)) as sound:
        assert sound.getframerate() == 22050 and sound.getnframes() == 22050
    with pytest.raises(ProviderUnavailable):
        asyncio.run(PiperSynthesizer(VoiceSettings(synthesis="piper", piper_executable=sys.executable,
                                                   piper_model=str(tmp_path / "missing.onnx"))).synthesize("Hola"))


def test_chains_use_the_next_engine_and_fail_closed():
    class Broken:
        async def transcribe(self, audio):
            raise ProviderUnavailable("down")

        async def synthesize(self, text):
            raise TimeoutError()

    class Working:
        async def transcribe(self, audio):
            return "Pregunta sintética"

        async def synthesize(self, text):
            return wav()

    assert asyncio.run(ChainTranscriber([Broken(), Working()]).transcribe(wav())) == "Pregunta sintética"
    assert asyncio.run(ChainSynthesizer([Broken(), Working()]).synthesize("x")) == wav()
    with pytest.raises(ProviderUnavailable):
        asyncio.run(ChainSynthesizer([Broken(), Broken()]).synthesize("x"))


def test_capabilities_name_external_providers_before_consent(guide, key):
    settings = VoiceSettings(transcription="groq", transcription_fallback=["windows_sapi"], synthesis="elevenlabs",
                             synthesis_fallback=["piper"], groq_api_key_file=key, elevenlabs_api_key_file=key,
                             elevenlabs_voice_id="SyntheticVoice01", piper_executable=sys.executable, piper_model="x.onnx")
    client = TestClient(create_app(guide[1], guide_service=guide[2], voice_service=VoiceService(guide[2], settings)))
    body = client.get("/api/v1/guide/voice-capabilities").json()
    assert body["processing"] == "external_providers" and body["providers"] == ["ElevenLabs", "Groq"]
    local = VoiceService(guide[2], VoiceSettings(synthesis="windows_sapi", windows_voice="Synthetic"))
    client = TestClient(create_app(guide[1], guide_service=guide[2], voice_service=local))
    assert client.get("/api/v1/guide/voice-capabilities").json()["providers"] == []


@pytest.mark.parametrize("patch", [{"transcription": "groq"}, {"synthesis": "elevenlabs", "elevenlabs_api_key_file": "k"},
                                   {"synthesis": "piper", "piper_model": "x"}, {"synthesis_fallback": ["piper"]},
                                   {"synthesis": "piper", "synthesis_fallback": ["piper"], "piper_executable": "p", "piper_model": "m"}])
def test_voice_chains_require_complete_explicit_engines(patch):
    with pytest.raises(ValidationError):
        VoiceSettings(**patch)



def test_provider_requests_identify_the_museum_agent(monkeypatch):
    # Regression: Groq's Cloudflare front rejected Python's default agent with 403 (real check 2026-10-20).
    seen = {}

    class Opener:
        def open(self, request, timeout):
            seen.update(agent=request.get_header("User-agent"), method=request.get_method())
            raise remote_http.URLError("offline test")

    monkeypatch.setattr(remote_http, "build_opener", lambda *handlers: Opener())
    with pytest.raises(ProviderUnavailable):
        remote_http.post("https://api.groq.com/openai/v1/chat/completions", b"{}", {}, 1, 10)
    assert seen == {"agent": "Musiyo-API/1.0", "method": "POST"}


def hedged_evidence():
    from types import SimpleNamespace
    return [SimpleNamespace(fragment_id="f1", title="Relato sintético", kind="summary", community=None, attribution=None,
                            context=None, source_reference="Fuente sintética",
                            text="Una de las versiones habla de una figura sintética cuyo regreso trajo cantos de prueba.")]


def test_lost_qualification_detection():
    from app.guide_hedges import lost_qualifications
    used = hedged_evidence()
    assert lost_qualifications("¿Qué es el relato?", "La figura sintética trajo cantos de prueba al regresar.", used)
    assert not lost_qualifications("¿Qué es el relato?", "Según una de las versiones, la figura sintética trajo cantos.", used)
    assert not lost_qualifications("¿Qué es el relato?", "Es un relato sintético.", used)


@pytest.mark.parametrize("repaired", [True, False])
def test_dropped_qualifier_gets_one_repair_then_falls_back(key, repaired):
    answers = ["La figura sintética trajo cantos de prueba al regresar.",
               "Según una de las versiones, la figura sintética trajo cantos de prueba." if repaired
               else "La figura sintética regresó y trajo cantos de prueba."]
    calls = []

    def fake_post(url, payload, headers, timeout, limit):
        calls.append(payload["messages"])
        content = json.dumps({"answer": answers[len(calls) - 1], "fragment_ids": ["E1"], "sufficient": True})
        return {"choices": [{"message": {"content": content}}]}

    generation = GroqGeneration(groq_settings(key), post=fake_post)
    if repaired:
        result = generation.generate("¿Qué es el relato?", hedged_evidence())
        assert result.answer.startswith("Según una de las versiones") and len(calls) == 2
        assert "Una de las versiones habla" in calls[1][-1]["content"]
    else:
        with pytest.raises(ProviderUnavailable):
            generation.generate("¿Qué es el relato?", hedged_evidence())
        fallback = FallbackGeneration(GroqGeneration(groq_settings(key), post=fake_post), LocalExtractiveGeneration())
        calls.clear()
        assert "Una de las versiones" in fallback.generate("¿Qué es el relato?", hedged_evidence()).answer


def test_a_question_naming_one_record_is_scoped_to_its_most_specific_title():
    from app.guide_clarification import named_record
    titles = {"matachin": "El Matachín", "matachin-mask": "Máscara del Matachín", "bombo": "El bombo", "flauta": "La flauta"}
    assert named_record("¿Qué es Máscara del Matachín?", titles) == "matachin-mask"
    assert named_record("¿Quiénes son los matachines?", titles) == "matachin"
    assert named_record("¿Qué diferencia hay entre el bombo y la flauta?", titles) is None
    assert named_record("¿Qué instrumento suena?", titles) is None


def test_piper_speed_is_passed_as_length_scale():
    settings = VoiceSettings(synthesis="piper", piper_executable="piper.exe", piper_model="voz.onnx", piper_length_scale=1.15)
    command = PiperSynthesizer(settings).command(Path("piper.exe"), Path("voz.onnx"))
    assert command[-2:] == ["--length_scale", "1.15"]
    with pytest.raises(ValidationError):
        VoiceSettings(synthesis="piper", piper_executable="p", piper_model="m", piper_length_scale=2.5)


@pytest.mark.parametrize("text", ["Gracias por ver el video.", "¡Gracias por tu video!", "Subtítulos realizados por la comunidad de Amara.org",
                                  "Suscríbete al canal", "Gracias por ver."])
def test_groq_silence_phrases_are_not_questions(monkeypatch, key, text):
    monkeypatch.setattr(voice_adapters, "post_multipart", lambda *args: {"text": text})
    settings = VoiceSettings(transcription="groq", synthesis="disabled", groq_api_key_file=key)
    with pytest.raises(ProviderUnavailable):
        asyncio.run(GroqTranscriber(settings).transcribe(wav()))


@pytest.mark.parametrize("text", ["Gracias por explicarme el bombo, ¿qué es el cacho?", "¿Por qué se ve el video del Castillo?",
                                  "Hola, ¿me podrías explicar de qué trata el museo?"])
def test_real_questions_are_kept(text):
    assert not voice_adapters.silence_phrase(text)
