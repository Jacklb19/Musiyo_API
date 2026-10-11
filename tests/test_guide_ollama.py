"""Synthetic adapter tests; fake outputs are not language-model quality evidence."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from test_guide import guide as guide
from test_guide import question

from app.content_operations import import_package, reindex
from app.content_tables import CONTENT
from app.guide_adapters import LocalExtractiveGeneration, ProviderUnavailable
from app.guide_config import GuideSettings
from app.guide_ollama import LocalOllamaTransport, OllamaEmbeddings, OllamaGeneration
from app.guide_repository import index_evidence
from app.guide_service import GuideService

DIGEST = "a" * 64


def settings(**changes):
    return GuideSettings(embeddings="ollama", generation="ollama", ollama_embedding_digest=DIGEST,
                         ollama_generation_digest=DIGEST, **changes)


@pytest.mark.parametrize("value", ["https://example.com:443", "http://10.0.0.1:11434",
    "http://127.0.0.1:11434/path", "http://user@localhost:11434", "http://localhost:11434?target=x",
    "http://localhost", "http://localhost:11434#fragment"])
def test_external_or_ambiguous_endpoint_is_rejected(value):
    with pytest.raises(ValidationError):
        settings(ollama_url=value)


def test_models_require_local_tags_and_pinned_digests():
    for change in [{"generation": "ollama"}, {"embeddings": "ollama"},
                   {"ollama_generation_model": "qwen3:4b-cloud"}]:
        with pytest.raises(ValidationError):
            GuideSettings(**change)


class FakeTransport:
    def __init__(self, result):
        self.result, self.requests = result, []
    def verify(self, model, digest, deadline):
        assert digest == DIGEST
    def request(self, path, payload, deadline):
        self.requests.append((path, payload))
        return self.result


@pytest.mark.parametrize("vectors", [[], [[0.0] * 1024], [[1.0] * 3],
    [[float('nan')] * 1024], [[True] * 1024], [["x"] * 1024]])
def test_malformed_embedding_cannot_enter_the_index(vectors):
    with pytest.raises(ProviderUnavailable):
        OllamaEmbeddings(settings(), FakeTransport({"embeddings": vectors})).embed(["Synthetic text"])


def test_embedding_is_normalized_and_query_has_retrieval_instruction():
    transport = FakeTransport({"embeddings": [[2.0] + [0.0] * 1023]})
    adapter = OllamaEmbeddings(settings(), transport)
    assert adapter.embed_query("Synthetic question") == [1.0] + [0.0] * 1023
    payload = transport.requests[0][1]
    assert payload['truncate'] is False and payload['dimensions'] == 1024
    assert payload['input'][0].startswith('Instruct:') and payload['input'][0].endswith('Synthetic question')
    assert DIGEST in adapter.model


def test_model_replacement_or_remote_model_rejected_before_content(monkeypatch):
    transport = LocalOllamaTransport(settings())
    calls = []
    def changed(path, payload, deadline):
        calls.append(path)
        return {"models": [{"name": "qwen3:4b", "digest": "b" * 64}]}
    monkeypatch.setattr(transport, 'request', changed)
    with pytest.raises(ProviderUnavailable):
        transport.verify('qwen3:4b', DIGEST, time.monotonic() + 1)
    assert calls == ['/api/tags']
    def remote(path, payload, deadline):
        return ({"models": [{"name": "qwen3:4b", "digest": DIGEST}]} if path == '/api/tags'
                else {"remote_host": "https://example.com"})
    monkeypatch.setattr(transport, 'request', remote)
    with pytest.raises(ProviderUnavailable):
        transport.verify('qwen3:4b', DIGEST, time.monotonic() + 1)


def test_transport_rejects_redirect_and_deadline_without_contacting_proxy(monkeypatch):
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header('Location', 'https://example.com/never-contact')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
        transport = LocalOllamaTransport(settings(ollama_url=f'http://127.0.0.1:{server.server_port}'))
        with pytest.raises(ProviderUnavailable):
            transport.request('/api/tags', None, time.monotonic() + 1)
        with pytest.raises(ProviderUnavailable):
            transport.request('/api/tags', None, time.monotonic() - 1)
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def evidence_for(guide):
    _, factory, baseline, _, _ = guide
    from app.guide_repository import GuideRepository
    with factory() as db:
        return GuideRepository(db).retrieve(question().question, None, baseline.embeddings, baseline.settings)


@pytest.mark.parametrize('content', ['not-json', '{"sufficient":"yes","answer":"x","fragment_ids":[]}',
    '{"sufficient":true,"answer":"x","fragment_ids":[],"extra":"no"}'])
def test_invalid_structured_generation_is_provider_unavailable(guide, content):
    adapter = OllamaGeneration(settings(), FakeTransport({'done': True, 'message': {'content': content}}))
    with pytest.raises(ProviderUnavailable):
        adapter.generate(question().question, evidence_for(guide))


def test_structured_generation_cannot_fabricate_citations(guide):
    _, factory, _, _, _ = guide
    payload = {'sufficient': True, 'answer': 'Respuesta sintética.', 'fragment_ids': ['fabricated']}
    transport = FakeTransport({'done': True, 'message': {'content': json.dumps(payload)}})
    adapter = OllamaGeneration(settings(), transport)
    result = GuideService(factory, generation=adapter).ask(question())
    assert result.status == 'abstained' and result.answer is None and result.citations == []
    request = transport.requests[0][1]
    assert request['think'] is False and request['stream'] is False
    assert 'source_reference' in json.loads(request['messages'][1]['content'])['evidence'][0]


class SyntheticEmbeddings:
    """Deterministic test vectors only, not an evaluated semantic model."""
    model = 'synthetic-semantic-v1'
    dimension = 1024
    def embed(self, texts):
        return [[1.0] + [0.0] * 1023 for text in texts]


def test_semantic_paraphrase_model_isolation_and_failed_reindex_rollback(guide):
    _, factory, baseline, _, _ = guide
    adapter = SyntheticEmbeddings()
    with factory.begin() as db:
        index_evidence(db, embeddings=adapter)
    semantic = GuideService(factory, embeddings=adapter, generation=LocalExtractiveGeneration())
    assert semantic.ask(question('¿Cómo se escucha esta pieza?')).status == 'answered'
    assert baseline.ask(question()).status == 'abstained'  # Different models never share vectors.
    class Failure(SyntheticEmbeddings):
        def embed(self, texts):
            raise ProviderUnavailable('Synthetic provider outage')
    with pytest.raises(ProviderUnavailable), factory.begin() as db:
        index_evidence(db, embeddings=Failure())
    assert semantic.ask(question()).status == 'answered'


def test_corrections_invalidate_semantic_index_without_calling_provider(guide, monkeypatch, tmp_path):
    url, factory, _, manifest, value = guide
    monkeypatch.setenv('MUSIYO_GUIDE_EMBEDDINGS', 'ollama')
    monkeypatch.setenv('MUSIYO_GUIDE_OLLAMA_EMBEDDING_DIGEST', DIGEST)
    value['elements'][0]['record']['blocks'][0]['text'] = 'Texto sintético corregido.'
    manifest.write_text(json.dumps(value), encoding='utf-8')
    import_package(manifest, url, private_storage=tmp_path / 'private')
    with factory() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CONTENT['index_fragments'])) == 0
    monkeypatch.setattr('app.guide_providers.configured_embeddings', lambda settings: SyntheticEmbeddings())
    assert reindex(url)['embedding_model'] == 'synthetic-semantic-v1'


def test_busy_model_returns_typed_unavailable_without_waiting(guide):
    _, factory, _, _, _ = guide
    service = GuideService(factory, settings=settings(), embeddings=SyntheticEmbeddings(),
                           generation=LocalExtractiveGeneration())
    service.model_lock.acquire()
    try:
        result = service.ask(question())
        assert result.status == 'provider_unavailable' and result.answer is None
    finally:
        service.model_lock.release()


@pytest.mark.skipif(not os.getenv('MUSIYO_TEST_POSTGRES_URL'), reason='Dedicated PostgreSQL URL required')
def test_postgresql_semantic_model_persistence_and_revocation(guide, tmp_path):
    from sqlalchemy.engine import make_url

    from app.cli import import_test_data
    from app.content_operations import revoke
    from app.db import make_session_factory
    from app.migrations import upgrade_database
    _, _, _, manifest, _ = guide
    url = os.environ['MUSIYO_TEST_POSTGRES_URL']
    assert (make_url(url).database or '').startswith('musiyo_verify_')
    upgrade_database(url)
    import_test_data(url)
    import_package(manifest, url, private_storage=tmp_path / 'pg-private')
    factory = make_session_factory(url)
    with factory.begin() as db:
        index_evidence(db, embeddings=SyntheticEmbeddings())
    factory.kw['bind'].dispose()
    with factory() as db:
        rows = db.execute(sa.select(CONTENT['index_fragments'])).mappings().all()
        assert rows and all(row['embedding_model'] == 'synthetic-semantic-v1' for row in rows)
        assert all(len(row['embedding']) == 1024 and row['search_vector'] for row in rows)
    service = GuideService(factory, embeddings=SyntheticEmbeddings(), generation=LocalExtractiveGeneration())
    assert service.ask(question('¿Cómo se escucha esta pieza?')).status == 'answered'
    revoke('synthetic-import', database_url=url)
    assert service.ask(question()).status == 'abstained'



def test_external_configuration_is_validated_and_environment_can_override(tmp_path, monkeypatch):
    path = tmp_path / 'guide-settings.json'
    path.write_text(json.dumps(settings().model_dump()), encoding='utf-8')
    monkeypatch.setenv('MUSIYO_GUIDE_CONFIG', str(path))
    assert GuideSettings.from_environment().ollama_embedding_digest == DIGEST
    monkeypatch.setenv('MUSIYO_GUIDE_GENERATION', 'disabled')
    assert GuideSettings.from_environment().generation == 'disabled'
    path.write_text('{"ollama_url":"http://example.com:11434"}', encoding='utf-8')
    with pytest.raises(ValidationError):
        GuideSettings.from_environment()



def test_generation_uses_only_supplied_short_references_and_maps_current_ids(guide):
    evidence = evidence_for(guide)
    payload = {'sufficient': True, 'answer': 'Respuesta de prueba.', 'fragment_ids': ['E1']}
    transport = FakeTransport({'done': True, 'message': {'content': json.dumps(payload)}})
    result = OllamaGeneration(settings(), transport).generate(question().question, evidence)
    assert result.fragment_ids == (evidence[0].fragment_id,)
    request = transport.requests[0][1]
    allowed = request['format']['properties']['fragment_ids']['items']['enum']
    assert allowed == [f'E{index + 1}' for index in range(len(evidence))]
    supplied = json.loads(request['messages'][1]['content'])['evidence']
    assert supplied[0]['fragment_id'] == 'E1'
