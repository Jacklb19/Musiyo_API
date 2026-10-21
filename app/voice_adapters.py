"""In-memory voice adapters: local Windows/Piper engines and owner-selected Groq/ElevenLabs.

Audio and text stay in memory for one request. Provider keys are read from external files.
"""
import asyncio
import base64
import io
import json
import os
import wave
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Protocol

from .guide_adapters import ProviderUnavailable
from .remote_http import post, post_multipart, read_secret
from .voice_config import VoiceSettings

MAX_OUTPUT_BYTES = 3000000
MAX_OUTPUT_SECONDS = 90
OUTPUT_RATES = (16000, 22050, 24000)
# Generic Spanish question openings; they bias dictation without adding cultural content.
QUESTION_FRAMES = ("qué es", "qué son", "quién es", "quiénes son", "cuál es", "cuáles son", "qué significa",
                   "qué significan", "cómo", "cuándo", "dónde", "por qué", "para qué", "cuántos", "cuántas")


def vocabulary_terms(titles: Iterable[str]) -> list[str]:
    """Published titles and their longer words, bounded for the recognizer grammar."""
    terms: set[str] = set()
    for title in titles:
        title = " ".join(title.split())
        if 1 <= len(title) <= 80:
            terms.add(title)
        terms.update(word for word in (part.strip("¿?¡!:;,.«»()\"'") for part in title.split()) if 3 < len(word) <= 40)
    return sorted(terms)[:200]


def validate_wave(data: bytes, max_seconds: int, max_bytes: int, *, input_audio: bool = True):
    """Visitor input must be PCM16 mono 16 kHz; synthesized output may also use 22.05/24 kHz."""
    if not data or len(data) > max_bytes:
        raise ValueError("Audio size is outside the limit")
    try:
        with wave.open(io.BytesIO(data)) as audio:
            rate = audio.getframerate()
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getcomptype() != "NONE"
                    or rate not in ((16000,) if input_audio else OUTPUT_RATES)):
                raise ValueError("Select mono PCM16 at 16000 Hz")
            frames = audio.getnframes()
            if frames < (rate // 10 if input_audio else 1) or frames > rate * max_seconds:
                raise ValueError("Audio duration is outside the limit")
            if len(audio.readframes(frames)) != frames * 2:
                raise ValueError("Truncated audio")
    except (wave.Error, EOFError) as error:
        raise ValueError("Invalid WAV") from error


def pcm_wave(pcm: bytes, rate: int = 16000) -> bytes:
    if not pcm or len(pcm) % 2 or len(pcm) > MAX_OUTPUT_BYTES or rate not in OUTPUT_RATES:
        raise ValueError("Invalid PCM output")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm)
    return output.getvalue()


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes) -> str: ...


class Synthesizer(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


class DisabledVoice:
    async def transcribe(self, audio: bytes) -> str:
        raise ProviderUnavailable("Transcription disabled")

    async def synthesize(self, text: str) -> bytes:
        raise ProviderUnavailable("Synthesis disabled")


class WhisperCppTranscriber:
    def __init__(self, settings: VoiceSettings):
        self.settings = settings

    async def transcribe(self, audio: bytes) -> str:
        try:
            # Optional 'voice' extra; the verified container runtime does not install it.
            import httpx
        except ImportError as error:
            raise ProviderUnavailable("Transcription client unavailable") from error
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                         timeout=self.settings.engine_timeout_seconds) as client:
                async with client.stream("POST", self.settings.whisper_url + "/inference",
                                         files={"file": ("question.wav", audio, "audio/wav")},
                                         data={"language": "es", "response_format": "json",
                                               "temperature": "0.0"}) as response:
                    if response.status_code != 200:
                        raise ProviderUnavailable("Transcription unavailable")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 4096:
                            raise ProviderUnavailable("Transcription response too large")
            result = json.loads(content)
            text = result.get("text") if isinstance(result, dict) else None
            if not isinstance(text, str) or not 3 <= len(text.strip()) <= 500:
                raise ProviderUnavailable("Invalid transcription")
            return text.strip()
        except (httpx.HTTPError, ValueError, UnicodeError) as error:
            raise ProviderUnavailable("Transcription unavailable") from error


async def run_windows_script(name: str, payload: bytes, timeout: int, max_output: int) -> bytes:
    """Run a bundled System.Speech script with data on stdin; no arguments, files or network."""
    if os.name != "nt":
        raise ProviderUnavailable("Local Windows speech unavailable")
    executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    process = await asyncio.create_subprocess_exec(
        str(executable), "-NoProfile", "-NonInteractive", "-File", str(Path(__file__).with_name(name)),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=0x08000000)
    try:
        output, _ = await asyncio.wait_for(process.communicate(payload), timeout)
        if process.returncode != 0 or len(output) > max_output:
            raise ProviderUnavailable("Local Windows speech unavailable")
        return base64.b64decode(output.strip(), validate=True)
    except (ValueError, TimeoutError) as error:
        raise ProviderUnavailable("Local Windows speech unavailable") from error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class WindowsSapiSynthesizer:
    def __init__(self, settings: VoiceSettings):
        self.settings = settings

    async def synthesize(self, text: str) -> bytes:
        if not 1 <= len(text) <= 2000:
            raise ProviderUnavailable("Local synthesis unavailable")
        # Text goes through stdin as data, never a command argument or a temporary file.
        payload = json.dumps({"text": text, "voice": self.settings.windows_voice}).encode("ascii")
        pcm = await run_windows_script("synthesize-local.ps1", payload, self.settings.engine_timeout_seconds, 4000000)
        try:
            # The script emits raw PCM in the fixed format it selects; wrap it as WAV here.
            audio = pcm_wave(pcm)
            validate_wave(audio, MAX_OUTPUT_SECONDS, MAX_OUTPUT_BYTES, input_audio=False)
        except ValueError as error:
            raise ProviderUnavailable("Local synthesis unavailable") from error
        return audio


class WindowsSapiTranscriber:
    def __init__(self, settings: VoiceSettings, vocabulary: Callable[[], Awaitable[list[str]]] | None = None):
        self.settings = settings
        self.vocabulary = vocabulary

    async def transcribe(self, audio: bytes) -> str:
        terms = await self.vocabulary() if self.vocabulary else []
        # The WAV was validated by the route; it travels base64-encoded on stdin only.
        payload = json.dumps({"audio": base64.b64encode(audio).decode("ascii"),
                              "culture": self.settings.windows_recognizer_culture,
                              "frames": list(QUESTION_FRAMES), "vocabulary": terms}).encode("ascii")
        output = await run_windows_script("recognize-local.ps1", payload, self.settings.engine_timeout_seconds, 8192)
        try:
            text = " ".join(output.decode("utf-8").split())
        except UnicodeError as error:
            raise ProviderUnavailable("Invalid transcription") from error
        if not 3 <= len(text) <= 500:
            raise ProviderUnavailable("Invalid transcription")
        return text


class GroqTranscriber:
    URL = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(self, settings: VoiceSettings):
        self.settings = settings

    async def transcribe(self, audio: bytes) -> str:
        key = read_secret(self.settings.groq_api_key_file)
        result = await asyncio.to_thread(
            post_multipart, self.URL, {"model": self.settings.groq_transcription_model, "language": "es",
                                       "response_format": "json", "temperature": "0"},
            ("question.wav", audio, "audio/wav"), {"Authorization": "Bearer " + key},
            self.settings.engine_timeout_seconds, 65536)
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or not 3 <= len(" ".join(text.split())) <= 500:
            raise ProviderUnavailable("Invalid transcription")
        return " ".join(text.split())


class ElevenLabsSynthesizer:
    URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000"

    def __init__(self, settings: VoiceSettings):
        self.settings = settings

    async def synthesize(self, text: str) -> bytes:
        if not 1 <= len(text) <= 2000:
            raise ProviderUnavailable("Synthesis text outside limits")
        key = read_secret(self.settings.elevenlabs_api_key_file)
        body = json.dumps({"text": text, "model_id": self.settings.elevenlabs_model_id}, ensure_ascii=False).encode("utf-8")
        pcm = await asyncio.to_thread(
            post, self.URL.format(voice=self.settings.elevenlabs_voice_id), body,
            {"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/pcm"},
            self.settings.engine_timeout_seconds, MAX_OUTPUT_BYTES)
        try:
            # pcm_16000 is raw signed 16-bit little-endian mono; wrap it as the WAV clients expect.
            audio = pcm_wave(pcm)
            validate_wave(audio, MAX_OUTPUT_SECONDS, MAX_OUTPUT_BYTES, input_audio=False)
        except ValueError as error:
            raise ProviderUnavailable("Invalid synthesized audio") from error
        return audio


class PiperSynthesizer:
    """Local Piper voice (free fallback). Text goes on stdin; raw PCM comes back on stdout."""

    def __init__(self, settings: VoiceSettings):
        self.settings = settings

    def selected(self) -> tuple[Path, Path, int]:
        try:
            executable = Path(self.settings.piper_executable or "").resolve(strict=True)
            model = Path(self.settings.piper_model or "").resolve(strict=True)
            rate = json.loads(Path(str(model) + ".json").read_text(encoding="utf-8"))["audio"]["sample_rate"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ProviderUnavailable("Piper is not installed as configured") from error
        if rate not in OUTPUT_RATES:
            raise ProviderUnavailable("Unsupported Piper voice sample rate")
        return executable, model, rate

    def command(self, executable: Path, model: Path) -> list[str]:
        return [str(executable), "--model", str(model), "--output_raw"]

    async def synthesize(self, text: str) -> bytes:
        if not 1 <= len(text) <= 2000:
            raise ProviderUnavailable("Synthesis text outside limits")
        executable, model, rate = self.selected()
        process = await asyncio.create_subprocess_exec(
            *self.command(executable, model),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == "nt" else 0)
        try:
            # One line of text; newlines would start separate utterances.
            line = " ".join(text.split()).encode("utf-8") + b"\n"
            output, _ = await asyncio.wait_for(process.communicate(line), self.settings.engine_timeout_seconds)
            if process.returncode != 0:
                raise ProviderUnavailable("Piper failed")
            audio = pcm_wave(output, rate)
            validate_wave(audio, MAX_OUTPUT_SECONDS, MAX_OUTPUT_BYTES, input_audio=False)
            return audio
        except (ValueError, TimeoutError) as error:
            raise ProviderUnavailable("Piper output unavailable") from error
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


class ChainTranscriber:
    """Try engines in the configured order; the first usable transcription wins."""

    def __init__(self, engines: list[Transcriber]):
        self.engines = engines

    async def transcribe(self, audio: bytes) -> str:
        for engine in self.engines:
            try:
                return await engine.transcribe(audio)
            except (ProviderUnavailable, TimeoutError, OSError):
                continue
        raise ProviderUnavailable("No transcription engine available")


class ChainSynthesizer:
    def __init__(self, engines: list[Synthesizer]):
        self.engines = engines

    async def synthesize(self, text: str) -> bytes:
        for engine in self.engines:
            try:
                return await engine.synthesize(text)
            except (ProviderUnavailable, TimeoutError, OSError):
                continue
        raise ProviderUnavailable("No synthesis engine available")
