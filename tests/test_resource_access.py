from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from app.content_operations import import_package
from app.content_tables import CONTENT
from app.contracts import ResourceAccess
from app.db import TourRecord, make_session_factory
from app.main import create_app
from app.public_repository import PublicRepository
from app.resource_storage import S3Storage
from tests.test_content_operations import package_fixture
from tests.test_public_api import NOW, client_with_data


def test_automatic_transfer_cannot_export_to_remote_storage(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSIYO_LOCAL_COMPOSE", raising=False)
    client = Mock()
    client.meta = SimpleNamespace(endpoint_url="https://storage.example.test")
    with pytest.raises(ValueError, match="limited to local MinIO"):
        S3Storage(client, "test-bucket").upload_local(tmp_path / "file", "objects/test", "text/plain", "a" * 64)
    client.upload_file.assert_not_called()


def test_local_minio_import_preserves_mime_hash_and_private_cache_without_uploading_on_dry_run(tmp_path, monkeypatch):
    url, manifest, value = package_fixture(tmp_path)
    client = Mock()
    client.meta = SimpleNamespace(endpoint_url="http://127.0.0.1:9000")
    monkeypatch.setattr("app.content_operations.configured_s3", lambda: S3Storage(client, "test-private"))
    storage = tmp_path / "private"
    import_package(manifest, url, dry_run=True, private_storage=storage)
    client.upload_file.assert_not_called()
    import_package(manifest, url, private_storage=storage)
    args, keywords = client.upload_file.call_args
    assert args[1] == "test-private" and args[2].startswith("objects/")
    assert keywords["ExtraArgs"] == {"ContentType":"audio/wav", "CacheControl":"private, max-age=300",
        "Metadata":{"sha256":value["files"]["narration-test"]["sha256"]}}


def test_local_delivery_expires_at_five_minutes_and_supports_range_requests(tmp_path):
    client_with_data(tmp_path)
    current = [NOW]
    client = TestClient(create_app(make_session_factory(f"sqlite:///{tmp_path / 'test.db'}"),
        now=lambda: current[0], private_storage=tmp_path / "storage", resource_signing_key=b't' * 32))
    response = client.post("/api/v1/resources/autorizado/access")
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    access = ResourceAccess.model_validate_json(response.content)
    assert access.expires_at == NOW + timedelta(seconds=300)
    assert "private_path" not in response.text
    delivered = client.get(access.url, headers={"Range": "bytes=0-3"})
    assert delivered.status_code == 206 and delivered.content == b"cont"
    current[0] = NOW + timedelta(seconds=299)
    assert client.get(access.url).status_code == 200
    current[0] = NOW + timedelta(seconds=300)
    assert client.get(access.url).status_code == 404
    assert client.post("/api/v1/resources/autorizado/access").status_code == 200


def test_signature_is_bound_to_resource_and_expiration_and_revocation(tmp_path):
    client = client_with_data(tmp_path)
    response = client.post("/api/v1/resources/autorizado/access")
    url = response.json()["url"]
    assert client.get(url.replace("/autorizado?", "/sin-aprobar?")).status_code == 404
    parsed = parse_qs(urlsplit(url).query)
    expires = parsed["expires"][0]
    assert client.get(url.replace("expires=" + expires, "expires=" + str(int(expires) + 1))).status_code == 404
    assert client.get(url.replace("signature=" + parsed["signature"][0], "signature=" + "0" * 64)).status_code == 404
    factory = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    with factory.begin() as db:
        db.execute(CONTENT["authorizations"].update().where(CONTENT["authorizations"].c.resource_id == "autorizado").values(revoked_at=NOW))
    assert client.get(url).status_code == 404
    assert client.post("/api/v1/resources/autorizado/access").status_code == 404


@pytest.mark.parametrize("resource", ["sin-aprobar", "de-draft", "unknown", "fuera"])
def test_access_never_signs_unavailable_resources_or_outside_paths(tmp_path, resource):
    client = client_with_data(tmp_path)
    assert client.post(f"/api/v1/resources/{resource}/access").status_code == 404
    assert client.post("/api/v1/resources/autorizado/access?schema_version=2").status_code == 409


def s3_client():
    return boto3.client("s3", endpoint_url="https://storage.example.test", region_name="us-east-1",
        aws_access_key_id="SYNTHETIC_TEST_KEY", aws_secret_access_key="SYNTHETIC_TEST_SECRET",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))


def test_s3_signs_get_for_300_seconds_after_checking_the_private_object():
    client = s3_client()
    resource = {"private_path": "objects/test.bin", "mime": "application/octet-stream", "sha256": "a" * 64, "byte_count": 4}
    with Stubber(client) as stub:
        stub.add_response("head_object", {"ContentLength": 4, "Metadata": {"sha256": "a" * 64}},
            {"Bucket": "test-private", "Key": "objects/test.bin"})
        url = S3Storage(client, "test-private").access(resource, 300)
        query = parse_qs(urlsplit(url).query)
        assert query["X-Amz-Expires"] == ["300"] and query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert query["response-cache-control"] == ["private, max-age=300"]
        assert urlsplit(url).hostname == "storage.example.test"
        stub.assert_no_pending_responses()


def test_s3_checks_resource_hash_and_uses_explicit_cors_origin():
    client = s3_client()
    storage = S3Storage(client, "test-private")
    with Stubber(client) as stub:
        stub.add_response("head_object", {"ContentLength": 4, "Metadata": {"sha256": "b" * 64}},
            {"Bucket": "test-private", "Key": "objects/test.bin"})
        assert storage.access({"private_path":"objects/test.bin", "mime":"application/octet-stream", "sha256":"a" * 64}, 300) is None
        stub.add_response("put_bucket_cors", {}, {"Bucket":"test-private", "CORSConfiguration":{"CORSRules":[{
            "AllowedOrigins":["https://museum.example.test"], "AllowedMethods":["GET", "HEAD"],
            "AllowedHeaders":["Range"], "ExposeHeaders":["Content-Length", "Content-Range", "Accept-Ranges"], "MaxAgeSeconds":300}]}})
        storage.configure_cors("https://museum.example.test")
    for origin in ("*", "https://*.example.test", "https://museum.example.test/path", "https://user@museum.example.test"):
        with pytest.raises(ValueError):
            storage.configure_cors(origin)


def test_storage_failure_is_masked_and_does_not_prevent_catalog_use(tmp_path):
    client_with_data(tmp_path)
    storage = Mock(spec=S3Storage)
    storage.access.side_effect = ValueError("SENSITIVE INTERNAL CONFIGURATION")
    client = TestClient(create_app(make_session_factory(f"sqlite:///{tmp_path / 'test.db'}"), now=lambda:NOW, object_storage=storage))
    response = client.post("/api/v1/resources/autorizado/access")
    assert response.status_code == 503 and response.json()["code"] == "service_unavailable"
    assert "SENSITIVE" not in response.text and client.get("/api/v1/catalog").status_code == 200
    storage.access.reset_mock()
    assert client.post("/api/v1/resources/de-draft/access").status_code == 404
    storage.access.assert_not_called()


def test_resource_lookup_requires_own_current_grant_and_current_parent(tmp_path):
    client_with_data(tmp_path)
    factory = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    with factory() as db:
        public = PublicRepository(db, now=lambda: NOW)
        assert public.resource_by_id("autorizado") is not None
        for denied in ("sin-aprobar", "de-draft", "unknown"):
            assert public.resource_by_id(denied) is None
    with factory.begin() as db:
        db.execute(CONTENT["authorizations"].update().where(CONTENT["authorizations"].c.element_id == "visible").values(revoked_at=NOW))
    with factory() as db:
        assert PublicRepository(db, now=lambda: NOW).resource_by_id("autorizado") is None


@pytest.mark.parametrize("parent", ["room_id", "point_id"])
def test_resource_lookup_excludes_retired_tour_parents(tmp_path, parent):
    client_with_data(tmp_path)
    factory = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    with factory.begin() as db:
        db.execute(CONTENT["resources"].update().where(CONTENT["resources"].c.id == "autorizado")
            .values(**{parent: "room-prueba" if parent == "room_id" else "point-01"}))
        db.get(TourRecord, "recorrido-prueba").published = False
    with factory() as db:
        assert PublicRepository(db, now=lambda: NOW).resource_by_id("autorizado") is None
