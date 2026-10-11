import pytest

from app.resource_storage import S3Storage


@pytest.mark.parametrize('explicit', [False, True])
def test_s3_accepts_sdk_role_chain_or_explicit_local_credentials(monkeypatch, explicit):
    for key in ['MUSIYO_S3_ENDPOINT', 'MUSIYO_S3_PUBLIC_ENDPOINT', 'MUSIYO_S3_ACCESS_KEY', 'MUSIYO_S3_SECRET_KEY']:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('MUSIYO_S3_BUCKET', 'synthetic-private')
    calls = []
    monkeypatch.setattr('app.resource_storage.boto3.client', lambda *args, **kwargs: calls.append(kwargs) or object())
    if explicit:
        monkeypatch.setenv('MUSIYO_S3_ENDPOINT', 'http://127.0.0.1:9000')
        monkeypatch.setenv('MUSIYO_S3_ACCESS_KEY', 'synthetic-access')
        monkeypatch.setenv('MUSIYO_S3_SECRET_KEY', 'synthetic-secret')
    S3Storage.from_environment()
    assert len(calls) == 2
    assert ('aws_access_key_id' in calls[0]) == explicit
    assert calls[0]['endpoint_url'] == ('http://127.0.0.1:9000' if explicit else None)


def test_incomplete_explicit_s3_credentials_are_rejected(monkeypatch):
    monkeypatch.setenv('MUSIYO_S3_BUCKET', 'synthetic-private')
    monkeypatch.setenv('MUSIYO_S3_ACCESS_KEY', 'synthetic-access')
    monkeypatch.delenv('MUSIYO_S3_SECRET_KEY', raising=False)
    with pytest.raises(ValueError, match='paired'):
        S3Storage.from_environment()
