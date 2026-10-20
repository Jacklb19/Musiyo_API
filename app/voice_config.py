"""Explicit local voice selection. Startup never downloads or starts engines."""
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class VoiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    transcription: Literal["disabled", "whisper_cpp", "windows_sapi"] = "disabled"
    synthesis: Literal["disabled", "windows_sapi"] = "disabled"
    whisper_url: str = "http://127.0.0.1:8189"
    windows_voice: str | None = Field(default=None, min_length=1, max_length=120)
    windows_recognizer_culture: str = Field(default="es-ES", pattern=r"^es-[A-Z]{2}$")
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

    @model_validator(mode="after")
    def explicit_voice(self):
        if self.synthesis == "windows_sapi" and not self.windows_voice:
            raise ValueError("Select an installed Spanish voice explicitly")
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
