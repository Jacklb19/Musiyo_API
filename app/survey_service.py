"""Opinion questions of the closing conversation, stored anonymously only with the visitor's consent.

The questions come from a reviewed JSON file (MUSIYO_SURVEY_BUNDLE; by default the bundle shipped in
deployment/survey). A stored response holds the survey key and version, the platform, the chosen
options and the day, nothing else: no time, address, identifier or free text. Until the
0006 migration has created the table, the questions are still offered and storing answers
reports that it is unavailable, so the API can be deployed before the database is migrated.
"""
import json
import os
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError, ProgrammingError

from .db import utc_now
from .survey_contracts import GuideSurvey, GuideSurveyResponse

DEFAULT_BUNDLE = Path(__file__).resolve().parents[1] / "deployment" / "survey" / "museum-main-v3.json"
CONSENT = "anonymous-v1"

SURVEY_RESPONSES = sa.Table(
    "survey_responses", sa.MetaData(),
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("survey_key", sa.String(100), nullable=False),
    sa.Column("version", sa.String(100), nullable=False),
    sa.Column("answered_on", sa.Date(), nullable=False),
    sa.Column("platform", sa.String(10)),
    sa.Column("answers", sa.JSON(), nullable=False),
)


def bundle_path() -> Path | None:
    configured = os.environ.get("MUSIYO_SURVEY_BUNDLE")
    if configured is None:
        return DEFAULT_BUNDLE
    return Path(configured) if configured.strip() else None


class SurveyUnavailable(Exception):
    """The answers cannot be stored right now (no table yet, or the database failed)."""


class SurveyService:
    def __init__(self, factory, path: Path | None = None, now=utc_now):
        self.factory, self.path, self.now = factory, path, now

    def survey(self, tour_key: str) -> GuideSurvey | None:
        if self.path is None or not self.path.is_file():
            return None
        survey = GuideSurvey.model_validate(json.loads(self.path.read_text(encoding="utf-8-sig")))
        return survey if survey.tour_key == tour_key else None

    def store(self, response: GuideSurveyResponse) -> None:
        survey = self.current(response)
        options = {question.id: {option.id for option in question.options} for question in survey.questions}
        for answer in response.answers:
            if answer.option_id not in options.get(answer.question_id, set()):
                raise ValueError("Unknown question or option")
        row = dict(id=str(uuid4()), survey_key=response.survey_key, version=response.version,
                   answered_on=self.now().date(), platform=response.platform,
                   answers={answer.question_id: answer.option_id for answer in response.answers})
        try:
            with self.factory.begin() as db:
                db.execute(SURVEY_RESPONSES.insert().values(**row))
        except (OperationalError, ProgrammingError) as error:
            raise SurveyUnavailable() from error

    def current(self, response: GuideSurveyResponse) -> GuideSurvey:
        if self.path is None or not self.path.is_file():
            raise ValueError("No survey is published")
        survey = GuideSurvey.model_validate(json.loads(self.path.read_text(encoding="utf-8-sig")))
        if (survey.survey_key, survey.version) != (response.survey_key, response.version):
            raise ValueError("The survey changed; reload it")
        return survey
