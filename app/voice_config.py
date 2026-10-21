"""Explicit voice engine selection. Startup never downloads, starts or contacts an engine.

Each direction has a primary engine and an ordered fallback list (owner decision 2026-10-20:
Groq Whisper to understand; ElevenLabs to speak, with Piper as the free fallback).
"""
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Transcriber = Literal["whisper_cpp", "windows_sapi", "groq"]
Synthesizer = Literal["windows_sapi", "elevenlabs", "piper"]
# Public names shown to visitors before they accept sending audio or text outside the museum server.
ProviderName = Literal["Groq", "ElevenLabs"]
EXTERNAL_PROVIDERS: dict[str, ProviderName] = {"groq": "Groq", "elevenlabs": "ElevenLabs"}


class VoiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    transcription: Literal["disabled"] | Transcriber = "disabled"
    transcription_fallback: list[Transcriber] = Field(default_factory=list, max_length=2)
    synthesis: Literal["disabled"] | Synthesizer = "disabled"
    synthesis_fallback: list[Synthesizer] = Field(default_factory=list, max_length=2)
    whisper_url: str = "http://127.0.0.1:8189"
    windows_voice: str | None = Field(default=None, min_length=1, max_length=120)
    windows_recognizer_culture: str = Field(default="es-ES", pattern=r"^es-[A-Z]{2}$")
    groq_api_key_file: str | None = Field(default=None, min_length=1, max_length=500)
    groq_transcription_model: str = Field(default="whisper-large-v3-turbo", pattern=r"^[a-z0-9][a-z0-9._/-]{2,99}$")
    elevenlabs_api_key_file: str | None = Field(default=None, min_length=1, max_length=500)
    elevenlabs_voice_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9]{10,40}$")
    elevenlabs_model_id: str = Field(default="eleven_multilingual_v2", pattern=r"^[a-z0-9_]{3,60}$")
    piper_executable: str | None = Field(default=None, min_length=1, max_length=500)
    piper_model: str | None = Field(default=None, min_length=1, max_length=500)
    max_input_bytes: int = Field(default=5000000, ge=1000, le=5000000)
    max_input_seconds: int = Field(default=30, ge=1, le=30)
    engine_timeout_seconds: int = Field(default=15, ge=1, le=20)
    request_timeout_seconds: int = Field(default=45, ge=5, le=55)

    @field_validator("whisper_url")
    @classmethod
    def loopback_only(cls, value):
        url = urlsplit(value)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1"}
                or url.port is None or url.username or url.password or url.query
                or url.fragment or url.path not in {"", "/"}):
            raise ValueError("Select an explicit numeric loopback HTTP port")
        return value.rstrip("/")

    @property
    def transcription_chain(self) -> list[str]:
        return [] if self.transcription == "disabled" else [self.transcription, *self.transcription_fallback]

    @property
    def synthesis_chain(self) -> list[str]:
        return [] if self.synthesis == "disabled" else [self.synthesis, *self.synthesis_fallback]

    @property
    def external_providers(self) -> list[ProviderName]:
        used = self.transcription_chain + self.synthesis_chain
        return sorted({EXTERNAL_PROVIDERS[engine] for engine in used if engine in EXTERNAL_PROVIDERS})

    @model_validator(mode="after")
    def explicit_engines(self):
        for chain in (self.transcription_chain, self.synthesis_chain):
            if len(set(chain)) != len(chain):
                raise ValueError("An engine appears twice in the same chain")
        if (self.transcription == "disabled" and self.transcription_fallback) or (
                self.synthesis == "disabled" and self.synthesis_fallback):
            raise ValueError("A fallback needs an enabled primary engine")
        engines = set(self.transcription_chain + self.synthesis_chain)
        if "windows_sapi" in self.synthesis_chain and not self.windows_voice:
            raise ValueError("Select an installed Spanish voice explicitly")
        if "groq" in engines and not self.groq_api_key_file:
            raise ValueError("Groq needs an external key file")
        if "elevenlabs" in engines and not (self.elevenlabs_api_key_file and self.elevenlabs_voice_id):
            raise ValueError("ElevenLabs needs an external key file and a voice id")
        if "piper" in engines and not (self.piper_executable and self.piper_model):
            raise ValueError("Piper needs its installed executable and voice model")
        return self

    @classmethod
    def from_environment(cls):
        selected = os.environ.get("MUSIYO_VOICE_CONFIG")
        if not selected:
            return cls()
        path = Path(selected).resolve(strict=True)
        if path.is_relative_to(Path(__file__).resolve().parents[1]) or path.stat().st_size > 16384:
            raise ValueError("Voice settings must be a small external file")
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))
