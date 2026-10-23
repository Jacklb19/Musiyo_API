import csv
import json
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db import make_session_factory
from app.main import create_app
from app.migrations import upgrade_database
from app.survey_contracts import GuideSurvey
from app.survey_service import DEFAULT_BUNDLE, SURVEY_RESPONSES, SurveyService
from scripts.export_survey import export

NOW = datetime(2026, 10, 23, 15, 42, 7, tzinfo=timezone.utc)
CONSENT = {"X-Musiyo-Survey-Consent": "anonymous-v1"}


@pytest.fixture
def museum(tmp_path):
    url = f"sqlite:///{tmp_path / 'museum.db'}"
    upgrade_database(url)
    factory = make_session_factory(url)
    client = TestClient(create_app(factory, survey_service=SurveyService(factory, DEFAULT_BUNDLE, lambda: NOW)))
    return url, factory, client


def answers(**chosen):
    return dict(schema_version=1, survey_key="conversacion-final", version="v3", platform="quest",
                answers=[dict(question_id=q.replace("_", "-"), option_id=o) for q, o in chosen.items()])


def stored(factory):
    with factory() as db:
        return db.execute(sa.select(SURVEY_RESPONSES)).mappings().all()


def test_the_published_conversation_is_offered_to_everyone(museum):
    _, _, client = museum
    response = client.get("/api/v1/guide/survey", params={"tour_key": "museum-main"})
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    survey = GuideSurvey.model_validate(response.json())
    assert survey.version == "v3" and len(survey.questions) == 9
    assert survey.questions[0].text == "Para empezar, ¿te gustó tu visita?"
    assert client.get("/api/v1/guide/survey", params={"tour_key": "otro"}).status_code == 404
    assert client.get("/api/v1/guide/survey", params={"tour_key": "museum-main", "schema_version": 2}).status_code == 409


def test_answers_are_stored_only_with_consent_and_keep_only_the_day(museum):
    _, factory, client = museum
    body = answers(gusto="mucho", encontrar="casi-siempre", moverse="facil")
    assert client.post("/api/v1/guide/survey/responses", json=body).status_code == 403
    assert client.post("/api/v1/guide/survey/responses", json=body, headers={"X-Musiyo-Survey-Consent": "yes"}).status_code == 403
    assert stored(factory) == []
    response = client.post("/api/v1/guide/survey/responses", json=body, headers=CONSENT)
    assert response.status_code == 201 and response.json() == {"schema_version": 1, "status": "stored"}
    [row] = stored(factory)
    assert row["answered_on"] == NOW.date() and row["platform"] == "quest"
    assert row["answers"] == {"gusto": "mucho", "encontrar": "casi-siempre", "moverse": "facil"}
    assert set(row) == {"id", "survey_key", "version", "answered_on", "platform", "answers"}


@pytest.mark.parametrize("change", [
    dict(answers=[dict(question_id="gusto", option_id="muchisimo")]),
    dict(answers=[dict(question_id="no-existe", option_id="si")]),
    dict(version="v2"),
])
def test_answers_that_do_not_match_the_published_questions_are_refused(museum, change):
    _, factory, client = museum
    body = answers(gusto="mucho") | change
    assert client.post("/api/v1/guide/survey/responses", json=body, headers=CONSENT).status_code == 422
    assert stored(factory) == []


def test_free_text_and_repeated_answers_are_not_accepted(museum):
    _, factory, client = museum
    body = answers(gusto="mucho")
    body["comment"] = "Me llamo Ana"
    assert client.post("/api/v1/guide/survey/responses", json=body, headers=CONSENT).status_code == 422
    repeated = answers(gusto="mucho")
    repeated["answers"].append(dict(question_id="gusto", option_id="poco"))
    assert client.post("/api/v1/guide/survey/responses", json=repeated, headers=CONSENT).status_code == 422
    assert stored(factory) == []


def test_before_the_migration_the_questions_work_and_storing_reports_unavailable(museum):
    url, factory, client = museum
    with factory.begin() as db:
        db.execute(sa.text("DROP TABLE survey_responses"))
        db.execute(sa.text("UPDATE alembic_version SET version_num = '0005'"))
    assert client.get("/api/v1/ready").status_code == 200
    assert client.get("/api/v1/guide/survey", params={"tour_key": "museum-main"}).status_code == 200
    assert client.post("/api/v1/guide/survey/responses", json=answers(gusto="mucho"), headers=CONSENT).status_code == 503


def test_no_bundle_means_no_conversation(tmp_path):
    url = f"sqlite:///{tmp_path / 'museum.db'}"
    upgrade_database(url)
    factory = make_session_factory(url)
    client = TestClient(create_app(factory, survey_service=SurveyService(factory, None)))
    assert client.get("/api/v1/guide/survey", params={"tour_key": "museum-main"}).status_code == 404
    assert client.post("/api/v1/guide/survey/responses", json=answers(gusto="mucho"), headers=CONSENT).status_code == 422


def test_export_counts_each_option_in_offered_order_and_per_platform(museum, tmp_path):
    url, _, client = museum
    for platform, liked in (("quest", "mucho"), ("web", "mucho"), ("web", "poco")):
        body = answers(gusto=liked) | dict(platform=platform)
        assert client.post("/api/v1/guide/survey/responses", json=body, headers=CONSENT).status_code == 201
    survey = GuideSurvey.model_validate(json.loads(DEFAULT_BUNDLE.read_text(encoding="utf-8")))
    result = export(url, survey, tmp_path / "export")
    assert result["conversations"] == 3 and result["platforms"] == ["quest", "web"]
    with (tmp_path / "export" / "totales.csv").open(encoding="utf-8-sig") as stream:
        totals = {(row["pregunta"], row["opcion"]): row for row in csv.DictReader(stream)}
    assert totals[("gusto", "mucho")]["total"] == "2" and totals[("gusto", "mucho")]["web"] == "1"
    assert totals[("gusto", "poco")]["orden"] == "4" and totals[("gusto", "nada")]["total"] == "0"
    with (tmp_path / "export" / "respuestas.csv").open(encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["gusto"] for row in rows].count("mucho") == 2 and all(row["dia"] == "2026-10-23" for row in rows)
