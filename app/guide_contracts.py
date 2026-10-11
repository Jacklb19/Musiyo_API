"""Owner-approved additive guide v1 contract shared with Web and Unity."""
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .contracts import ContractModel, Identifier


class GuideQuestion(ContractModel):
    schema_version: Literal[1]
    question: str = Field(min_length=3, max_length=500)
    element_slug: Identifier | None = Field(default=None, max_length=100)

    @field_validator('question', mode='before')
    @classmethod
    def trim_question(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('schema_version', mode='before')
    @classmethod
    def validate_version(cls, value):
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value


class GuideCitation(ContractModel):
    element_slug: Identifier
    title: str = Field(min_length=1)


class GuideResponse(ContractModel):
    model_config = ConfigDict(extra='forbid', strict=True, json_schema_extra={
        'allOf': [
            {'if': {'properties': {'status': {'const': 'answered'}}},
             'then': {'properties': {'answer': {'type': 'string', 'minLength': 1, 'pattern': r"\S"},
                                     'citations': {'minItems': 1}, 'reason': {'type': 'null'}}}},
            {'if': {'properties': {'status': {'const': 'abstained'}}},
             'then': {'properties': {'answer': {'type': 'null'}, 'citations': {'maxItems': 0},
                                     'reason': {'const': 'insufficient_evidence'}}}},
            {'if': {'properties': {'status': {'const': 'provider_unavailable'}}},
             'then': {'properties': {'answer': {'type': 'null'}, 'citations': {'maxItems': 0},
                                     'reason': {'const': 'provider_unavailable'}}}}
        ]})
    schema_version: Literal[1]
    status: Literal['answered', 'abstained', 'provider_unavailable']
    answer: str | None = Field(min_length=1, max_length=2000, pattern=r"\S")
    citations: list[GuideCitation] = Field(max_length=8)
    reason: Literal['insufficient_evidence', 'provider_unavailable'] | None

    @field_validator('schema_version', mode='before')
    @classmethod
    def validate_version(cls, value):
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value

    @model_validator(mode='after')
    def validate_state(self):
        if self.status == 'answered':
            if not self.answer or not self.answer.strip() or not self.citations or self.reason is not None:
                raise ValueError("Answered responses require evidence and no failure reason")
        else:
            if self.answer is not None or self.citations or self.reason is None:
                raise ValueError("Unanswered responses cannot contain an answer or evidence")
            provider_reasons = {'provider_unavailable'}
            if (self.status == 'provider_unavailable') != (self.reason in provider_reasons):
                raise ValueError("Response reason differs from its status")
        ids = [citation.element_slug for citation in self.citations]
        if len(ids) != len(set(ids)):
            raise ValueError("Repeated evidence citation")
        return self
