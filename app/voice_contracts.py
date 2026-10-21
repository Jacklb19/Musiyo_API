"""Additive voice v1 wire models; textual guide v1 remains unchanged."""
from typing import Literal

from pydantic import Field, model_validator

from .contracts import ContractModel
from .guide_contracts import GuideResponse


class VoiceCapabilities(ContractModel):
    schema_version: Literal[1] = 1
    transcription: Literal["disabled", "configured"]
    synthesis: Literal["disabled", "configured"]
    # external_providers: audio or text also goes to the named providers (shown before consent).
    processing: Literal["backend_local", "external_providers"] = "backend_local"
    providers: list[Literal["Groq", "ElevenLabs"]] = Field(default_factory=list, max_length=2)
    retention: Literal["request_only"] = "request_only"
    input_mime: Literal["audio/wav"] = "audio/wav"
    max_input_bytes: int
    max_input_seconds: int
    request_timeout_seconds: int


class GuideTranscription(ContractModel):
    schema_version: Literal[1] = 1
    text: str = Field(min_length=3, max_length=500)


class GuideSpeech(ContractModel):
    mime: Literal["audio/wav"] = "audio/wav"
    data_base64: str = Field(min_length=1, max_length=4000000)


class GuideVoiceAnswer(ContractModel):
    schema_version: Literal[1] = 1
    response: GuideResponse
    speech_status: Literal["ready", "disabled", "unavailable", "not_applicable"]
    audio: GuideSpeech | None

    @model_validator(mode="after")
    def consistent_audio(self):
        if (self.speech_status == "ready") != (self.audio is not None):
            raise ValueError("Only ready speech may contain audio")
        if self.response.status != "answered" and self.speech_status != "not_applicable":
            raise ValueError("Only a grounded answer may be spoken")
        if self.response.status == "answered" and self.speech_status == "not_applicable":
            raise ValueError("Answered responses must report synthesis state")
        return self
