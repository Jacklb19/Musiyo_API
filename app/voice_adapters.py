"""In-memory local-engine adapters. No remote fallback or provider credentials."""
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
from .voice_config import VoiceSettings

MAX_OUTPUT_BYTES = 3000000
MAX_OUTPUT_SECONDS = 90
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
    if not data or len(data) > max_bytes:
        raise ValueError("Audio size is outside the limit")
    try:
        with wave.open(io.BytesIO(data)) as audio:
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                    or audio.getframerate() != 16000 or audio.getcomptype() != "NONE"):
                raise ValueError("Select mono PCM16 at 16000 Hz")
            frames = audio.getnframes()
            if frames < (1600 if input_audio else 1) or frames > 16000 * max_seconds:
                raise ValueError("Audio duration is outside the limit")
            if len(audio.readframes(frames)) != frames * 2:
                raise ValueError("Truncated audio")
    except (wave.Error, EOFError) as error:
        raise ValueError("Invalid WAV") from error


def pcm_wave(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % 2 or len(pcm) > MAX_OUTPUT_BYTES:
        raise ValueError("Invalid PCM output")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
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
