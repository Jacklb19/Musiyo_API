import json
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from test_guide import guide  # noqa: F401

from app.content_tables import CONTENT
from app.guide_config import GuideSettings
from app.guide_service import GuideService
from app.main import create_app
from app.quiz_contracts import GuideQuiz, GuideQuizAnswer, GuideQuizAnswerResult
from app.quiz_service import QuizBundle, QuizService, generate_drafts, write_new_bundle


@pytest.fixture
def quiz(guide, tmp_path):  # noqa: F811
    import copy

    from app.content_operations import import_package

    url, factory, _, manifest, value = guide
    for index in range(2):
        extra = copy.deepcopy(value)
        record = extra['elements'][0]['record']
        record.update(slug=f'synthetic-extra-{index}', title=f'Ficha sintética {index}', resources=[])
        record['blocks'][0].update(id=f'block-extra-{index}', text=f'Hecho sintético número {index}.')
        extra['files'] = {}
        manifest.write_text(json.dumps(extra), encoding='utf-8')
        import_package(manifest, url, private_storage=tmp_path / 'private')
    with factory.begin() as db:
        db.execute(CONTENT['detail_blocks'].update().values(kind='documented_fact'))
    bundle = generate_drafts(factory, 'recorrido-prueba')
    assert bundle.questions
    path = tmp_path / 'quiz.json'
    write_new_bundle(path, bundle)
    service = QuizService(factory, path)
    app = create_app(factory, quiz_service=service)
    return TestClient(app), bundle, path, factory


def reviewed(bundle, path):
    value = bundle.model_dump()
    for draft in value['questions']:
        draft.update(state='reviewed', reviewed_by='Synthetic reviewer', reviewed_at=datetime.now(timezone.utc).isoformat())
    path.write_text(json.dumps(value), encoding='utf-8')


def test_drafts_never_publish_and_answers_are_hidden_until_submission(quiz):
    client, bundle, path, _ = quiz
    query = '/api/v1/guide/quiz?tour_key=recorrido-prueba'
    question = bundle.questions[0].question
    assert client.get(query).json()['questions'] == []
    body = dict(schema_version=1, id=question.id, option_id='a')
    assert client.post('/api/v1/guide/quiz/answers', json=body).status_code == 404
    reviewed(bundle, path)
    result = client.get(query)
    assert result.status_code == 200
    assert result.headers['cache-control'] == 'private, no-store'
    assert 'correct_option_id' not in result.text and 'explanation' not in result.text
    assert client.get(query + '&room_key=missing').json()['questions'] == []
    for option, expected in [(bundle.questions[0].correct_option_id, True),
                              (next(item.id for item in question.options if item.id != bundle.questions[0].correct_option_id), False)]:
        answer = client.post('/api/v1/guide/quiz/answers', json=body | {'option_id': option})
        assert answer.status_code == 200
        assert answer.json()['correct'] is expected
        assert answer.json()['source']['id'] == question.source_id
        assert answer.json()['citation']['quote']
    assert client.post('/api/v1/guide/quiz/answers', json=body | {'option_id': 'unknown'}).status_code == 422


@pytest.mark.parametrize('change', ['permission', 'correction', 'source', 'withdrawal'])
def test_current_authorization_and_exact_evidence_are_required(quiz, change):
    client, bundle, path, factory = quiz
    reviewed(bundle, path)
    with factory.begin() as db:
        if change == 'permission':
            db.execute(CONTENT['authorizations'].update().values(allow_assistant=False))
        elif change == 'correction':
            db.execute(CONTENT['detail_blocks'].update().values(text='Corrected synthetic fact'))
        elif change == 'source':
            db.execute(CONTENT['sources'].update().values(reference='Corrected synthetic source'))
        else:
            db.execute(sa.text('update tours set published=0'))
    assert client.get('/api/v1/guide/quiz?tour_key=recorrido-prueba').json()['questions'] == []
    assert client.post('/api/v1/guide/quiz/answers', json=dict(schema_version=1,
        id=bundle.questions[0].question.id, option_id='a')).status_code == 404


def test_review_is_explicit_and_version_is_strict(quiz):
    _, bundle, _, _ = quiz
    value = bundle.model_dump()
    value['questions'][0]['state'] = 'reviewed'
    with pytest.raises(ValidationError):
        QuizBundle.model_validate(value)
    with pytest.raises(ValidationError):
        GuideQuizAnswer(schema_version=True, id='q', option_id='a')


def test_quiz_and_questions_share_the_same_limiter(quiz):
    _, _, _, factory = quiz
    guide_service = GuideService(factory, settings=GuideSettings(questions_per_minute=1))
    client = TestClient(create_app(factory, guide_service=guide_service))
    assert client.get('/api/v1/guide/quiz?tour_key=recorrido-prueba').status_code == 200
    response = client.post('/api/v1/guide/questions', json=dict(schema_version=1, question='Pregunta sintética'))
    assert response.status_code == 429 and response.headers['retry-after'] == '60'


def test_quiz_schemas_and_examples_are_canonical():
    from pathlib import Path
    contracts = Path(__file__).resolve().parents[1] / 'contracts'
    for name, model in [('guide-quiz', GuideQuiz), ('guide-quiz-answer', GuideQuizAnswer),
                        ('guide-quiz-answer-result', GuideQuizAnswerResult)]:
        schema = json.loads((contracts / (name + '.v1.schema.json')).read_text(encoding='utf-8'))
        assert schema == model.model_json_schema()
        example = json.loads((contracts / 'examples' / (name.replace('-', '_') + '.json')).read_text(encoding='utf-8'))
        Draft202012Validator(schema).validate(example)
        model.model_validate(example)
