from copy import deepcopy
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_content_operations import package_fixture

from app.content_operations import import_package, revoke
from app.content_tables import CONTENT
from app.db import TourRecord, make_session_factory, utc_now
from app.main import create_app


def test_resource_only_import_preserves_existing_resources_records_grants_links_and_index(tmp_path):
    import json

    url, manifest, value = package_fixture(tmp_path)
    storage = tmp_path / 'private'
    import_package(manifest, url, private_storage=storage)
    factory = make_session_factory(url)
    with factory() as db:
        before_index = db.execute(sa.select(CONTENT['index_fragments'])).mappings().all()
        parent_grants = db.execute(sa.select(CONTENT['authorizations']).where(CONTENT['authorizations'].c.element_id == 'synthetic-import')).mappings().all()
        revision = db.get(TourRecord, 'recorrido-prueba').revision
    appended = deepcopy(value)
    appended['elements'][0]['record']['resources'][0]['id'] = 'narration-added'
    appended['files']['narration-added'] = appended['files'].pop('narration-test')
    manifest.write_text(json.dumps(appended), encoding='utf-8')
    result = import_package(manifest, url, private_storage=storage, resources_only=True, dry_run=True)
    assert result['tour_revisions']['recorrido-prueba']
    with factory() as db:
        assert db.get(TourRecord, 'recorrido-prueba').revision == revision
    import_package(manifest, url, private_storage=storage, resources_only=True)
    with factory() as db:
        assert db.execute(sa.select(CONTENT['index_fragments'])).mappings().all() == before_index
        assert db.execute(sa.select(CONTENT['authorizations']).where(CONTENT['authorizations'].c.element_id == 'synthetic-import')).mappings().all() == parent_grants
        assert db.get(TourRecord, 'recorrido-prueba').revision is not None
    current = [utc_now()]
    client = TestClient(create_app(factory, private_storage=storage, now=lambda: current[0], resource_signing_key=b'n' * 32))
    record = client.get('/api/v1/elements/synthetic-import').json()
    assert {r['id'] for r in record['resources']} == {'narration-test', 'narration-added'}
    access = client.post('/api/v1/resources/narration-added/access').json()
    assert client.get(access['url'], headers={'Range': 'bytes=0-3'}).status_code == 206
    current[0] += timedelta(seconds=301)
    assert client.get(access['url']).status_code == 404
    revoke('narration-added', resource=True, database_url=url)
    assert client.post('/api/v1/resources/narration-added/access').status_code == 404
    assert client.get('/api/v1/elements/synthetic-import').status_code == 200
    with pytest.raises(ValueError, match='existing resource'):
        import_package(manifest, url, private_storage=storage, resources_only=True)
    appended['elements'][0]['record']['title'] = 'Changed record'
    manifest.write_text(json.dumps(appended), encoding='utf-8')
    with pytest.raises(ValueError, match='exact current'):
        import_package(manifest, url, private_storage=storage, resources_only=True)
