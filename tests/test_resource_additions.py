import hashlib
import json
import struct
from copy import deepcopy
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_content_operations import package_fixture

from app.content_operations import import_package, revoke
from app.content_tables import CONTENT
from app.db import PointElementRecord, ResourceRecord, TourRecord, make_session_factory, utc_now
from app.main import create_app
from app.resource_additions import append_resources
from app.timed_text import parse_vtt


def subtitle_fixture(tmp_path):
    url, manifest, initial = package_fixture(tmp_path)
    storage = tmp_path / 'private'
    import_package(manifest, url, private_storage=storage)
    vtt = tmp_path / 'captions.vtt'
    vtt.write_text('WEBVTT\n\n00:00:00.000 --> 00:00:00.900\nTEST TRANSCRIPT\n', encoding='utf-8')
    with make_session_factory(url)() as db:
        narration = db.get(ResourceRecord, 'narration-test')
        sha = narration.sha256
    value = {'operation_version': 1, 'decision': initial['decision'], 'additions': [{
        'element_slug': 'synthetic-import', 'resource': {'id': 'captions-new', 'kind': 'subtitles', 'mime': 'text/vtt',
            'credit': 'Synthetic generator', 'provenance': 'Synthetic measured silence'},
        'file': {'file': 'captions.vtt', 'sha256': hashlib.sha256(vtt.read_bytes()).hexdigest()}}],
        'subtitle_links': [{'narration_id': 'narration-test', 'expected_sha256': sha, 'subtitles_id': 'captions-new'}]}
    patch = tmp_path / 'additions.json'
    patch.write_text(json.dumps(value), encoding='utf-8')
    return url, storage, patch, value


def snapshot(url):
    with make_session_factory(url)() as db:
        return {name: db.execute(sa.select(CONTENT[name])).mappings().all() for name in
            ['authorizations', 'detail_blocks', 'restrictions', 'credits', 'element_sources', 'index_fragments']}


def test_subtitle_append_links_atomically_preserves_grants_and_obeys_access_expiry_and_revocation(tmp_path):
    url, storage, patch, value = subtitle_fixture(tmp_path)
    before = snapshot(url)
    factory = make_session_factory(url)
    with factory() as db:
        revision = db.get(TourRecord, 'recorrido-prueba').revision
    result = append_resources(patch, url, private_storage=storage, dry_run=True)
    assert result['subtitle_links'] == 1
    assert snapshot(url) == before
    with factory() as db:
        assert db.get(ResourceRecord, 'captions-new') is None
        assert db.get(ResourceRecord, 'narration-test').subtitles_resource_id is None
        assert db.get(TourRecord, 'recorrido-prueba').revision == revision
    append_resources(patch, url, private_storage=storage)
    after = snapshot(url)
    assert after['authorizations'][:len(before['authorizations'])] == before['authorizations']
    assert {k:v for k,v in after.items() if k != 'authorizations'} == {k:v for k,v in before.items() if k != 'authorizations'}
    clock = [utc_now()]
    client = TestClient(create_app(factory, private_storage=storage, now=lambda: clock[0], resource_signing_key=b'x' * 32))
    record = client.get('/api/v1/elements/synthetic-import').json()
    assert next(r for r in record['resources'] if r['id'] == 'narration-test')['subtitles_resource_id'] == 'captions-new'
    access = client.post('/api/v1/resources/captions-new/access').json()
    response = client.get(access['url'])
    assert response.status_code == 200 and response.headers['content-type'].startswith('text/vtt')
    assert hashlib.sha256(response.content).hexdigest() == value['additions'][0]['file']['sha256']
    assert client.get(access['url'], headers={'Range': 'bytes=0-5'}).status_code == 206
    assert client.get(access['url'][:-1] + ('0' if access['url'][-1] != '0' else '1')).status_code == 404
    clock[0] += timedelta(seconds=300)
    assert client.get(access['url']).status_code == 404
    clock[0] = utc_now()
    access = client.post('/api/v1/resources/captions-new/access').json()
    revoke('captions-new', resource=True, database_url=url)
    assert client.get(access['url']).status_code == 404
    assert client.post('/api/v1/resources/captions-new/access').status_code == 404
    record = client.get('/api/v1/elements/synthetic-import').json()
    assert record['resources'][0]['subtitles_resource_id'] is None
    with pytest.raises(ValueError, match='Existing resource'):
        append_resources(patch, url, private_storage=storage)


@pytest.mark.parametrize('failure', ['hash', 'wrong_narration', 'cross_element', 'changed_link', 'text', 'duration', 'overlap', 'unapproved', 'version'])
def test_invalid_links_roll_back_every_addition(tmp_path, failure):
    url, storage, patch, value = subtitle_fixture(tmp_path)
    if failure == 'version':
        value['operation_version'] = True
    elif failure == 'hash':
        value['subtitle_links'][0]['expected_sha256'] = '0' * 64
    elif failure == 'wrong_narration':
        value['subtitle_links'][0]['narration_id'] = 'missing'
    elif failure == 'cross_element':
        value['additions'][0]['element_slug'] = 'missing'
    elif failure == 'changed_link':
        value['subtitle_links'][0]['expected_subtitles_id'] = 'old-captions'
    elif failure == 'unapproved':
        value['decision']['approved_culturally'] = False
    else:
        text = {'text': 'WEBVTT\n\n00:00:00.000 --> 00:00:00.900\nALTERED\n',
            'duration': 'WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nTEST TRANSCRIPT\n',
            'overlap': 'WEBVTT\n\n00:00:00.000 --> 00:00:00.600\nTEST\n\n00:00:00.500 --> 00:00:00.900\nTRANSCRIPT\n'}[failure]
        (tmp_path / 'captions.vtt').write_text(text, encoding='utf-8')
        value['additions'][0]['file']['sha256'] = hashlib.sha256((tmp_path / 'captions.vtt').read_bytes()).hexdigest()
    patch.write_text(json.dumps(value), encoding='utf-8')
    before = snapshot(url)
    with pytest.raises(ValueError):
        append_resources(patch, url, private_storage=storage)
    assert snapshot(url) == before
    with make_session_factory(url)() as db:
        assert db.get(ResourceRecord, 'captions-new') is None
        assert db.get(ResourceRecord, 'narration-test').subtitles_resource_id is None


def glb(triangles=1):
    document = {'asset': {'version': '2.0'}, 'accessors': [{'count': triangles * 3}],
        'meshes': [{'primitives': [{'attributes': {'POSITION': 0}}]}]}
    data = json.dumps(document).encode()
    data += b' ' * (-len(data) % 4)
    return struct.pack('<4sIIII', b'glTF', 2, len(data) + 20, len(data), 0x4E4F534A) + data


def test_model_variant_append_preserves_web_and_rejects_same_bytes_or_over_budget(tmp_path):
    url, manifest, initial = package_fixture(tmp_path)
    resource = {'id': 'model-web', 'kind': 'model_3d', 'mime': 'model/gltf-binary', 'alternative_text': 'Synthetic triangle',
        'credit': 'Test', 'provenance': 'Synthetic'}
    (tmp_path / 'web.glb').write_bytes(glb(2))
    initial['elements'][0]['record']['resources'] = [resource]
    initial['files'] = {'model-web': {'file': 'web.glb', 'sha256': hashlib.sha256(glb(2)).hexdigest(), 'profile': 'web'}}
    manifest.write_text(json.dumps(initial), encoding='utf-8')
    storage = tmp_path / 'private'
    import_package(manifest, url, private_storage=storage)
    (tmp_path / 'quest.glb').write_bytes(glb())
    value = {'operation_version': 1, 'decision': initial['decision'], 'additions': [{
        'element_slug': 'synthetic-import', 'resource': {**resource, 'id': 'model-quest'},
        'expected_parent_sha256': initial['files']['model-web']['sha256'],
        'file': {'file': 'quest.glb', 'sha256': hashlib.sha256(glb()).hexdigest(), 'profile': 'quest', 'variant_of': 'model-web'}}]}
    patch = tmp_path / 'additions.json'
    for data in [glb(2), glb(40001)]:
        (tmp_path / 'quest.glb').write_bytes(data)
        value['additions'][0]['file']['sha256'] = hashlib.sha256(data).hexdigest()
        patch.write_text(json.dumps(value), encoding='utf-8')
        with pytest.raises(ValueError):
            append_resources(patch, url, private_storage=storage)
    (tmp_path / 'quest.glb').write_bytes(glb())
    value['additions'][0]['file']['sha256'] = hashlib.sha256(glb()).hexdigest()
    patch.write_text(json.dumps(value), encoding='utf-8')
    before = snapshot(url)
    append_resources(patch, url, private_storage=storage)
    client = TestClient(create_app(make_session_factory(url), private_storage=storage))
    models = client.get('/api/v1/elements/synthetic-import').json()['resources']
    assert sorted(models[0]['variants'], key=lambda row: row['id']) == [{'id': 'model-quest', 'profile': 'quest'}, {'id': 'model-web', 'profile': 'web'}]
    assert snapshot(url)['authorizations'][:len(before['authorizations'])] == before['authorizations']


def test_new_element_and_point_reassignment_are_one_transaction_and_preserve_old_records(tmp_path):
    url, manifest, initial = package_fixture(tmp_path)
    storage = tmp_path / 'private'
    import_package(manifest, url, private_storage=storage)
    new = deepcopy(initial)
    new['elements'][0]['record']['slug'] = 'new-exhibit'
    new['elements'][0]['record']['blocks'][0]['id'] = 'new-block'
    new['elements'][0]['record']['resources'][0]['id'] = 'new-narration'
    new['files']['new-narration'] = new['files'].pop('narration-test')
    new['elements'][0]['point_keys'] = []
    manifest.write_text(json.dumps(new), encoding='utf-8')
    plan = tmp_path / 'points.json'
    change = {'changes': [{'point_key': 'punto-01', 'expected': ['synthetic-import'], 'replacement': ['new-exhibit']}]}
    plan.write_text(json.dumps(change), encoding='utf-8')
    before = snapshot(url)
    import_package(manifest, url, private_storage=storage, new_elements_only=True, assignments=plan, dry_run=True)
    assert snapshot(url) == before
    change['changes'][0]['replacement'] = ['missing']
    plan.write_text(json.dumps(change), encoding='utf-8')
    with pytest.raises(ValueError, match='published'):
        import_package(manifest, url, private_storage=storage, new_elements_only=True, assignments=plan)
    assert snapshot(url) == before
    change['changes'][0]['replacement'] = ['new-exhibit']
    plan.write_text(json.dumps(change), encoding='utf-8')
    import_package(manifest, url, private_storage=storage, new_elements_only=True, assignments=plan)
    with make_session_factory(url)() as db:
        assert list(db.scalars(sa.select(PointElementRecord.element_id).where(PointElementRecord.point_id == 'punto-01'))) == ['new-exhibit']
        assert list(db.scalars(sa.select(PointElementRecord.element_id).where(PointElementRecord.point_id == 'punto-02'))) == ['synthetic-import']
    assert snapshot(url)['authorizations'][:len(before['authorizations'])] == before['authorizations']
    from app.resource_reversal import reverse_delivery
    reverse_delivery(manifest, url, assignments=plan, dry_run=True)
    reverse_delivery(manifest, url, assignments=plan)
    with make_session_factory(url)() as db:
        assert list(db.scalars(sa.select(PointElementRecord.element_id).where(PointElementRecord.point_id == 'punto-01'))) == ['synthetic-import']
    assert snapshot(url)['authorizations'][:len(before['authorizations'])] == before['authorizations']
    client = TestClient(create_app(make_session_factory(url), private_storage=storage))
    assert client.get('/api/v1/elements/new-exhibit').status_code == 404
    assert client.get('/api/v1/elements/synthetic-import').status_code == 200
    with pytest.raises(ValueError, match='replace existing'):
        import_package(manifest, url, private_storage=storage, new_elements_only=True, assignments=plan)


def test_plain_vtt_rejects_nonpositive_and_markup():
    for value in ['WEBVTT', 'WEBVTT\n\n00:00:01.000 --> 00:00:00.000\nx', 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<b>x</b>']:
        with pytest.raises(ValueError):
            parse_vtt(value)


def test_reversal_preserves_old_grants_and_index_and_never_reactivates_ids(tmp_path):
    from app.resource_reversal import reverse_delivery
    url, storage, patch, value = subtitle_fixture(tmp_path)
    before = snapshot(url)
    append_resources(patch, url, private_storage=storage)
    reverse_delivery(patch, url, dry_run=True)
    with make_session_factory(url)() as db:
        assert db.get(ResourceRecord, 'narration-test').subtitles_resource_id == 'captions-new'
    reverse_delivery(patch, url)
    after = snapshot(url)
    assert after['authorizations'][:len(before['authorizations'])] == before['authorizations']
    assert after['index_fragments'] == before['index_fragments']
    with make_session_factory(url)() as db:
        assert db.get(ResourceRecord, 'narration-test').subtitles_resource_id is None
    client = TestClient(create_app(make_session_factory(url), private_storage=storage))
    assert client.post('/api/v1/resources/captions-new/access').status_code == 404
    with pytest.raises(ValueError, match='Existing resource'):
        append_resources(patch, url, private_storage=storage)


def test_subtitle_preparation_refuses_unreviewed_audio_alignment(tmp_path):
    from scripts.prepare_subtitles import prepare_subtitles
    url, storage, patch, value = subtitle_fixture(tmp_path)
    review = tmp_path / 'review.json'
    review.write_text(json.dumps({'status': 'DRAFT-UNREVIEWED-DO-NOT-IMPORT', 'cues': []}), encoding='utf-8')
    decision = tmp_path / 'decision.json'
    decision.write_text(json.dumps(value['decision']), encoding='utf-8')
    output = tmp_path / 'reviewed/manifest.json'
    with pytest.raises(ValueError, match='Every phrase'):
        prepare_subtitles([review], decision, tmp_path / 'content.db', output)
    assert not output.exists()
