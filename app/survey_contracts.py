"""Additive v1 contracts for the closing conversation's opinion questions (owner decision D6, 22-23/10/2026).

The visitor answers by choosing options only, so nothing typed can identify them. A stored response
carries the survey version, the platform and the chosen options; the server adds only the day.
"""
from typing import Literal

from pydantic import Field, model_validator

from .contracts import ContractModel, Identifier


class SurveyOption(ContractModel):
    id: Identifier
    text: str = Field(min_length=1, max_length=120)


class SurveyQuestion(ContractModel):
    id: Identifier
    kind: Literal['single_choice'] = 'single_choice'
    text: str = Field(min_length=1, max_length=400)
    options: list[SurveyOption] = Field(min_length=2, max_length=8)
    # A short line the guide says after an answer, before the next question.
    reply: str | None = Field(default=None, max_length=200)
    optional: bool = False

    @model_validator(mode='after')
    def unique_options(self):
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError('Repeated option id')
        return self


class SurveyNotice(ContractModel):
    intro: str = Field(min_length=1, max_length=400)
    accept_label: str = Field(min_length=1, max_length=80)
    local_label: str = Field(min_length=1, max_length=80)
    decline_label: str = Field(min_length=1, max_length=80)
    more_label: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=1200)


class GuideSurvey(ContractModel):
    schema_version: Literal[1] = 1
    survey_key: Identifier
    version: Identifier
    tour_key: Identifier
    notice: SurveyNotice
    questions: list[SurveyQuestion] = Field(min_length=1, max_length=15)
    closing: str = Field(min_length=1, max_length=400)

    @model_validator(mode='after')
    def unique_questions(self):
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError('Repeated question id')
        return self


class SurveyAnswer(ContractModel):
    question_id: Identifier
    option_id: Identifier


class GuideSurveyResponse(ContractModel):
    schema_version: Literal[1]
    survey_key: Identifier
    version: Identifier
    platform: Literal['quest', 'web', 'desktop'] | None = None
    answers: list[SurveyAnswer] = Field(min_length=1, max_length=15)

    @model_validator(mode='after')
    def one_answer_per_question(self):
        if len({answer.question_id for answer in self.answers}) != len(self.answers):
            raise ValueError('Repeated question')
        return self


class GuideSurveyStored(ContractModel):
    schema_version: Literal[1] = 1
    status: Literal['stored'] = 'stored'
