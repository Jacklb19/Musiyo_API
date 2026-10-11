"""Validated, server-only configuration for the offline textual guide."""
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GuideSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    generation: Literal["local", "disabled"] = "local"
    candidate_limit: int = Field(default=8, ge=1, le=32)
    evidence_limit: int = Field(default=4, ge=1, le=8)
    minimum_coverage: float = Field(default=0.5, gt=0, le=1)
    questions_per_minute: int = Field(default=10, ge=1, le=60)
    source_limit: int = Field(default=4096, ge=16, le=100000)

    @classmethod
    def from_environment(cls):
        fields = {name: os.environ['MUSIYO_GUIDE_' + name.upper()] for name in cls.model_fields
                  if 'MUSIYO_GUIDE_' + name.upper() in os.environ}
        return cls.model_validate(fields)
