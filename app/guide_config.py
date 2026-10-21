"""Validated, server-only settings for offline and local model adapters."""
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class GuideSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    generation: Literal["local", "disabled", "ollama", "groq"] = "local"
    embeddings: Literal["local", "ollama"] = "local"
    candidate_limit: int = Field(default=8, ge=1, le=32)
    evidence_limit: int = Field(default=4, ge=1, le=8)
    minimum_coverage: float = Field(default=0.5, gt=0, le=1)
    minimum_similarity: float = Field(default=0.7, gt=0, le=1)
    questions_per_minute: int = Field(default=10, ge=1, le=60)
    source_limit: int = Field(default=4096, ge=16, le=100000)
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_embedding_model: str = "qwen3-embedding:0.6b"
    ollama_generation_model: str = "qwen3:4b"
    ollama_embedding_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    ollama_generation_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    ollama_timeout_seconds: float = Field(default=10, ge=1, le=40)
    ollama_context_tokens: int = Field(default=4096, ge=2048, le=8192)
    ollama_output_tokens: int = Field(default=384, ge=128, le=768)
    # External generation (owner decision 2026-10-20): questions and retrieved fragments go to Groq.
    groq_model: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._/-]{2,99}$")
    groq_api_key_file: str | None = Field(default=None, min_length=1, max_length=500)
    groq_timeout_seconds: float = Field(default=8, ge=1, le=20)
    groq_output_tokens: int = Field(default=600, ge=128, le=2000)
    # When the external provider fails, answer with the accepted local extractive guide.
    generation_fallback: Literal["local", "none"] = "local"

    @field_validator("ollama_url")
    @classmethod
    def local_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or parsed.port is None):
            raise ValueError("Ollama must use an explicit loopback HTTP port")
        return value.rstrip("/")

    @field_validator("ollama_embedding_model", "ollama_generation_model")
    @classmethod
    def local_model(cls, value: str) -> str:
        import re
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*", value) or "cloud" in value:
            raise ValueError("Select an explicitly tagged local model")
        return value

    @model_validator(mode="after")
    def pinned_models(self):
        if self.embeddings == "ollama" and self.ollama_embedding_digest is None:
            raise ValueError("Pin the installed embedding model digest before indexing")
        if self.generation == "ollama" and self.ollama_generation_digest is None:
            raise ValueError("Pin the installed generation model digest")
        if self.generation == "groq" and not (self.groq_model and self.groq_api_key_file):
            raise ValueError("Select the Groq model and the external key file explicitly")
        return self

    @classmethod
    def from_environment(cls):
        fields = {}
        config_path = os.getenv("MUSIYO_GUIDE_CONFIG")
        if config_path:
            path = Path(config_path).resolve()
            if path.is_relative_to(Path(__file__).resolve().parents[1]) or path.stat().st_size > 65536:
                raise ValueError("Guide configuration must be a small external JSON file")
            fields = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(fields, dict):
                raise ValueError("Guide configuration must be a JSON object")
        fields.update({name: os.environ['MUSIYO_GUIDE_' + name.upper()] for name in cls.model_fields
                       if 'MUSIYO_GUIDE_' + name.upper() in os.environ})
        return cls.model_validate(fields)
