"""Additive v1 quiz wire contracts; review metadata and answer keys stay private."""
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel, Identifier, Source


class QuizOption(ContractModel):
    id: Identifier
    text: str = Field(min_length=1, max_length=250)


class QuizQuestion(ContractModel):
    id: Identifier
    text: str = Field(min_length=1, max_length=1200)
    options: list[QuizOption] = Field(min_length=3, max_length=4)
    element_slug: Identifier
    source_id: Identifier

    @model_validator(mode='after')
    def unique_options(self):
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError('Repeated option id')
        if len({option.text.strip().casefold() for option in self.options}) != len(self.options):
            raise ValueError('Repeated option text')
        return self


class QuizVersion(ContractModel):
    schema_version: Literal[1]

    @field_validator('schema_version', mode='before')
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError('Schema version must be an integer')
        return value


class GuideQuiz(QuizVersion):
    schema_version: Literal[1]
    tour_key: Identifier
    room_key: Identifier | None
    questions: list[QuizQuestion] = Field(max_length=50)

    @model_validator(mode='after')
    def unique_questions(self):
        if len({item.id for item in self.questions}) != len(self.questions):
            raise ValueError('Repeated question id')
        return self


class GuideQuizAnswer(QuizVersion):
    schema_version: Literal[1]
    id: Identifier
    option_id: Identifier


class QuizCitation(ContractModel):
    element_slug: Identifier
    source_id: Identifier
    quote: str = Field(min_length=1, max_length=800)


class GuideQuizAnswerResult(QuizVersion):
    schema_version: Literal[1]
    id: Identifier
    correct: bool
    explanation: str = Field(min_length=1, max_length=1200)
    citation: QuizCitation
    source: Source
