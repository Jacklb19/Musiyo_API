import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from test_content_operations import package_fixture

from app.cli import import_test_data
from app.content_operations import import_package, reindex, revoke, verify_validity
from app.content_tables import CONTENT
from app.db import make_session_factory, utc_now
from app.guide_adapters import (
    GeneratedAnswer,
    LocalExtractiveGeneration,
    ProviderUnavailable,
)
from app.guide_config import GuideSettings
from app.guide_contracts import GuideQuestion, GuideResponse
from app.guide_routes import QuestionLimiter
from app.guide_service import GuideService
from app.main import create_app
from app.migrations import upgrade_database


@pytest.fixture
def guide(tmp_path):
    url, manifest, value = package_fixture(tmp_path)
    record = value['elements'][0]['record']
    record['title'] = 'Ficha sintética de instrumentos'
    record['description'] = 'Descripción sintética sin términos de búsqueda.'
    record['blocks'][0].update(text='El instrumento sintético produce sonido durante la prueba.', source_id='test-source')
    record['sources'] = [{'id': 'test-source', 'kind': 'other', 'reference': 'Referencia sintética de prueba'}]
    record['credits'] = [{'role': 'testing', 'name': 'Synthetic role'}]
    manifest.write_text(json.dumps(value), encoding='utf-8')
    import_package(manifest, url, private_storage=tmp_path / 'private')
    factory = make_session_factory(url)
    return url, factory, GuideService(factory), manifest, value


def question(text='¿Qué instrumento produce sonido?', slug=None):
    return GuideQuestion(schema_version=1, question=text, element_slug=slug)


def test_authorized_retrieval_has_only_current_citations_and_no_cache(guide):
    _, factory, service, _, _ = guide
    client = TestClient(create_app(factory, guide_service=service))
    response = client.post('/api/v1/guide/questions', json=question().model_dump())
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    result = response.json()
    assert set(result) == {'schema_version', 'status', 'answer', 'citations', 'reason'}
    assert result['status'] == 'answered' and 'instrumento sintético' in result['answer']
    assert result['citations'] == [{'element_slug': 'synthetic-import', 'title': 'Ficha sintética de instrumentos'}]
    assert result['reason'] is None


@pytest.mark.parametrize('change', ['denial', 'restriction', 'revoked', 'expired', 'future'])
def test_ineligible_content_cannot_be_retrieved_and_index_is_invalidated(guide, change):
    url, factory, service, _, _ = guide
    assert service.ask(question()).status == 'answered'
    if change == 'revoked':
        revoke('synthetic-import', database_url=url, reason='test')
    else:
        with factory.begin() as db:
            grants = CONTENT['authorizations']
            if change == 'restriction':
                db.execute(CONTENT['restrictions'].insert().values(element_id='synthetic-import',
                    kind='exclude_assistant', description='Synthetic exclusion'))
            else:
                from datetime import timedelta
                values = {'allow_assistant': False} if change == 'denial' else (
                    {'authorized_until': utc_now() - timedelta(seconds=1)} if change == 'expired' else
                    {'authorized_from': utc_now() + timedelta(days=1)})
                db.execute(grants.update().where(grants.c.element_id == 'synthetic-import').values(**values))
    assert service.ask(question()).model_dump() == dict(schema_version=1, status='abstained', answer=None,
                                                      citations=[], reason='insufficient_evidence')
    verify_validity(url)
    with factory() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CONTENT['index_fragments'])) == 0


def test_no_evidence_does_not_call_generation_and_context_grants_nothing(guide):
    _, factory, _, _, _ = guide
    class Uncalled:
        def generate(self, *args):
            pytest.fail('No evidence must not call generation')
    service = GuideService(factory, generation=Uncalled())
    assert service.ask(question('Astronautas galácticos')).status == 'abstained'
    assert service.ask(question(slug='unknown')).status == 'abstained'


@pytest.mark.parametrize('failure', ['generation', 'embeddings', 'disabled'])
def test_provider_failure_is_typed_and_catalog_remains_available(guide, failure):
    _, factory, _, _, _ = guide
    class Failing:
        model = 'local-hash-v1'
        dimension = 1024
        def generate(self, *args):
            raise ProviderUnavailable('internal provider detail')
        def embed(self, *args):
            raise TimeoutError('internal provider detail')
    service = GuideService(factory, settings=GuideSettings(generation='disabled') if failure == 'disabled' else None,
        generation=Failing() if failure == 'generation' else None,
        embeddings=Failing() if failure == 'embeddings' else None)
    client = TestClient(create_app(factory, guide_service=service))
    response = client.post('/api/v1/guide/questions', json=question().model_dump())
    assert response.status_code == 200
    assert response.json() == dict(schema_version=1, status='provider_unavailable', answer=None,
                                   citations=[], reason='provider_unavailable')
    assert client.get('/api/v1/catalog').status_code == 200
    assert client.get('/api/v1/tours/recorrido-prueba').status_code == 200


@pytest.mark.parametrize('mutation', ['revoke', 'edit', 'source'])
def test_revalidate_after_generation_prevents_disclosing_changed_content(guide, mutation):
    url, factory, _, _, _ = guide
    class Changing(LocalExtractiveGeneration):
        def generate(self, text, evidence):
            if mutation == 'revoke':
                revoke('synthetic-import', database_url=url)
            else:
                with factory.begin() as db:
                    table = CONTENT['detail_blocks' if mutation == 'edit' else 'sources']
                    db.execute(table.update().values(**({'text': 'Updated synthetic evidence'} if mutation == 'edit' else
                                                       {'reference': 'Changed source reference'})))
            return super().generate(text, evidence)
    assert GuideService(factory, generation=Changing()).ask(question()).status == 'abstained'


@pytest.mark.parametrize('output', [GeneratedAnswer(True, 'Invented answer', ('unknown',)),
    GeneratedAnswer(True, '', ()), GeneratedAnswer(True, 'word ' * 121, ('unknown',)),
    GeneratedAnswer(False, 'Must not be exposed', ()), {'sufficient': True}])
def test_invalid_generation_or_abstention_never_exposes_answer(guide, output):
    _, factory, _, _, _ = guide
    class Invalid:
        def generate(self, *args):
            return output
    result = GuideService(factory, generation=Invalid()).ask(question())
    assert result.status == 'abstained' and result.answer is None and result.citations == []


def test_indexed_correction_and_import_replace_evidence(guide, tmp_path):
    url, factory, service, manifest, value = guide
    value['elements'][0]['record']['blocks'][0]['text'] = 'El instrumento sintético produce sonido actualizado.'
    manifest.write_text(json.dumps(value), encoding='utf-8')
    import_package(manifest, url, private_storage=tmp_path / 'private')
    result = service.ask(question())
    assert result.status == 'answered' and 'actualizado' in result.answer
    with factory() as db:
        rows = db.execute(sa.select(CONTENT['index_fragments'])).mappings().all()
    assert len(rows) == 2 and all(len(row['embedding']) == 1024 for row in rows)
    assert reindex(url)['indexed_fragments'] == 2


def test_question_validation_and_rate_limit_are_separate_from_functional_states(guide):
    _, factory, service, _, _ = guide
    client = TestClient(create_app(factory, guide_service=service))
    for body in [{'schema_version': True, 'question': 'valid'}, {'schema_version': 2, 'question': 'valid'},
                 {'schema_version': 1, 'question': '  a  '}, {'schema_version': 1, 'question': 'x' * 501}]:
        assert client.post('/api/v1/guide/questions', json=body).status_code == 422
    assert question('  valid  ').question == 'valid'
    for _ in range(10):
        assert client.post('/api/v1/guide/questions', json=question().model_dump()).status_code == 200
    assert client.post('/api/v1/guide/questions', json=question().model_dump()).status_code == 429
    assert client.get('/api/v1/catalog').status_code == 200
    clock = [0.0]
    limiter = QuestionLimiter(1, 1, lambda: clock[0])
    assert limiter.accept('source-1') and not limiter.accept('source-2')
    assert 'source-1' not in limiter.sources
    clock[0] = 61.0
    assert limiter.accept('source-2')


def test_owner_contract_schemas_and_fixtures_are_exact():
    contracts = Path(__file__).resolve().parents[1] / 'contracts'
    for name, model in [('guide-question', GuideQuestion), ('guide-response', GuideResponse)]:
        schema = json.loads((contracts / f'{name}.v1.schema.json').read_text(encoding='utf-8'))
        assert schema == model.model_json_schema()
    response_schema = GuideResponse.model_json_schema()
    for name in ['guide_answered', 'guide_abstained', 'guide_unavailable']:
        value = json.loads((contracts / 'examples' / f'{name}.json').read_text(encoding='utf-8'))
        Draft202012Validator(response_schema).validate(value)
        assert GuideResponse.model_validate(value).model_dump() == value
    invalid = dict(schema_version=1, status='answered', answer=None, citations=[], reason=None)
    assert list(Draft202012Validator(response_schema).iter_errors(invalid))
    with pytest.raises(ValidationError):
        GuideResponse.model_validate(invalid)


@pytest.mark.skipif(not os.getenv('MUSIYO_TEST_POSTGRES_URL'), reason='Dedicated PostgreSQL URL required')
def test_postgresql_pgvector_persistence_and_revocation(guide, tmp_path):
    _, _, _, manifest, _ = guide
    url = os.environ['MUSIYO_TEST_POSTGRES_URL']
    from sqlalchemy.engine import make_url
    assert (make_url(url).database or '').startswith('musiyo_verify_'), 'Only isolated verification databases are allowed'
    upgrade_database(url)
    factory = make_session_factory(url)
    with factory() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CONTENT['elements'])) == 0
    import_test_data(url)
    import_package(manifest, url, private_storage=tmp_path / 'pg-private')
    service = GuideService(factory)
    assert service.ask(question()).status == 'answered'
    factory.kw['bind'].dispose()
    with factory() as db:
        rows = db.execute(sa.select(CONTENT['index_fragments'])).mappings().all()
        assert len(rows) == 2 and all(len(row['embedding']) == 1024 for row in rows)
        assert all(row['search_vector'] for row in rows)
    # Reconnection must retain vectors and the extension-backed index.
    assert service.ask(question()).status == 'answered'
    revoke('synthetic-import', database_url=url)
    assert service.ask(question()).status == 'abstained'
    with factory() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CONTENT['index_fragments'])) == 0
