import json
import secrets
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.content_operations import create_validator, import_package
from app.content_tables import CONTENT
from app.db import make_session_factory
from app.main import create_app
from app.validator_access import COOKIE_NAME, ValidatorAccess
from tests.test_content_operations import package_fixture
from tests.test_public_api import NOW


def validator_fixture(tmp_path):
    url, manifest, value = package_fixture(tmp_path)
    import_package(manifest, url, private_storage=tmp_path / "private")
    password = secrets.token_urlsafe(24)
    create_validator("test-validator", "Validador sintético", password, url)
    clock = [NOW]
    factory = make_session_factory(url)
    client = TestClient(create_app(factory, now=lambda:clock[0]), base_url="https://museum.example.test")
    return client, factory, password, clock


def login(client, password):
    response = client.post("/api/v1/auth/login", json={"username":"test-validator", "password":password})
    assert response.status_code == 200, response.text
    return response


def test_secure_session_cookie_expiration_and_hashed_server_identifier(tmp_path):
    client, factory, password, clock = validator_fixture(tmp_path)
    anonymous = client.get("/api/v1/auth/session").json()
    assert not anonymous["authenticated"] and anonymous["user"] is None and anonymous["csrf_token"] is None
    response = login(client, password)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie and "max-age=28800" in cookie
    token = client.cookies.get(COOKIE_NAME)
    assert token and password not in response.text
    with factory() as db:
        stored = db.execute(sa.select(CONTENT["validator_sessions"])).mappings().one()
        assert stored["id"] == ValidatorAccess.token_hash(token) and stored["id"] != token
    session = client.get("/api/v1/auth/session")
    assert session.json()["authenticated"] and session.headers["cache-control"] == "private, no-store"
    clock[0] += timedelta(hours=8)
    assert client.get("/api/v1/auth/session").json() == anonymous
    assert client.patch("/api/v1/validator/elements/synthetic-import", json={"title":"Test"},
        headers={"X-CSRF-Token":response.json()["csrf_token"]}).status_code == 401


def test_corrections_update_public_detail_search_and_index_atomically(tmp_path):
    client, factory, password, _ = validator_fixture(tmp_path)
    session = login(client, password).json()
    before = client.get("/api/v1/elements/synthetic-import").json()
    response = client.patch("/api/v1/validator/elements/synthetic-import", json={"title":"[PRUEBA] Edición sintética",
        "description":"Nueva descripción sintética", "interpretations":[{"block_id":"block-test", "text":"Interpretación corregida para pruebas"}]},
        headers={"X-CSRF-Token":session["csrf_token"]})
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["title"] == "[PRUEBA] Edición sintética" and record["blocks"][0]["text"] == "Interpretación corregida para pruebas"
    assert record["last_modified"]["author"] == "Validador sintético"
    for field in ("resources", "sources", "credits", "restrictions", "community", "category", "collections"):
        assert before[field] == record[field]
    assert client.get("/api/v1/catalog", params={"query":"edicion"}).json()["total"] == 1
    with factory() as db:
        assert db.scalar(sa.select(CONTENT["index_fragments"].c.text)) == "Interpretación corregida para pruebas"
        audit = db.execute(sa.select(CONTENT["validator_corrections"])).mappings().one()
        assert audit["fields"] == ["title", "description", "interpretations.block-test"]
        assert "Synthetic text" not in json.dumps(dict(audit), default=str)


@pytest.mark.parametrize("csrf", [None, "wrong", "é"])
def test_csrf_denies_mutations_without_changing_text(tmp_path, csrf):
    client, _, password, _ = validator_fixture(tmp_path)
    login(client, password)
    headers = {"X-CSRF-Token":csrf} if csrf else {}
    if csrf == "é":
        headers = {b"X-CSRF-Token":csrf.encode("latin-1")}
    response = client.patch("/api/v1/validator/elements/synthetic-import", json={"title":"Should not change"}, headers=headers)
    assert response.status_code == 403 and response.json()["code"] == "forbidden"
    assert client.get("/api/v1/elements/synthetic-import").json()["title"] == "[CONTENIDO DE PRUEBA — NO CULTURAL]"


@pytest.mark.parametrize("payload", [{"resources":[]}, {"sources":[]}, {"community":None}, {"title":None}, {}, {"title":"x" * 161},
    {"interpretations":[{"block_id":"block-test", "text":"x" * 3001}]},
    {"interpretations":[{"block_id":"block-test", "text":"Test"}, {"block_id":"block-test", "text":"Other"}]}])
def test_only_bounded_text_fields_are_editable(tmp_path, payload):
    client, factory, password, _ = validator_fixture(tmp_path)
    session = login(client, password).json()
    assert client.patch("/api/v1/validator/elements/synthetic-import", json=payload,
        headers={"X-CSRF-Token":session["csrf_token"]}).status_code == 422
    with factory() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CONTENT["validator_corrections"])) == 0


def test_block_membership_and_kind_are_checked_before_any_update(tmp_path):
    client, factory, password, _ = validator_fixture(tmp_path)
    session = login(client, password).json()
    with factory.begin() as db:
        db.execute(CONTENT["detail_blocks"].insert().values(id="fact-test",element_id="synthetic-import",
            kind="documented_fact",text="Documented test",order=1))
    for block in ("fact-test", "foreign-block"):
        response = client.patch("/api/v1/validator/elements/synthetic-import",json={"title":"Must roll back",
            "interpretations":[{"block_id":block,"text":"Invalid"}]},headers={"X-CSRF-Token":session["csrf_token"]})
        assert response.status_code == 422
    assert client.get("/api/v1/elements/synthetic-import").json()["title"] == "[CONTENIDO DE PRUEBA — NO CULTURAL]"


def test_retired_content_inactive_accounts_and_cross_origin_edits_are_denied(tmp_path):
    client, factory, password, _ = validator_fixture(tmp_path)
    session = login(client, password).json()
    headers = {"X-CSRF-Token":session["csrf_token"],"Origin":"https://other.example.test"}
    assert client.patch("/api/v1/validator/elements/synthetic-import",json={"title":"Blocked"},headers=headers).status_code == 403
    with factory.begin() as db:
        db.execute(CONTENT["authorizations"].update().where(CONTENT["authorizations"].c.element_id == "synthetic-import").values(revoked_at=NOW))
    assert client.patch("/api/v1/validator/elements/synthetic-import",json={"title":"Blocked"},headers={"X-CSRF-Token":session["csrf_token"]}).status_code == 403
    with factory.begin() as db:
        db.execute(CONTENT["validators"].update().values(active=False))
    assert not client.get("/api/v1/auth/session").json()["authenticated"]


def test_five_failed_attempts_lock_the_account_for_fifteen_minutes(tmp_path):
    client, factory, password, clock = validator_fixture(tmp_path)
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json={"username":"test-validator", "password":"wrong"}).status_code == 401
    assert client.post("/api/v1/auth/login",json={"username":"test-validator","password":password}).status_code == 401
    clock[0] += timedelta(minutes=14, seconds=59)
    assert client.post("/api/v1/auth/login",json={"username":"test-validator","password":password}).status_code == 401
    clock[0] += timedelta(seconds=1)
    login(client, password)
    with factory() as db:
        account = db.execute(sa.select(CONTENT["validators"])).mappings().one()
        assert account["failed_attempts"] == 0 and account["locked_until"] is None


def test_rate_limit_is_shared_across_unknown_accounts_and_resets_after_a_minute(tmp_path):
    client, _, password, clock = validator_fixture(tmp_path)
    for index in range(10):
        assert client.post("/api/v1/auth/login",json={"username":f"unknown-{index}","password":"wrong"}).status_code == 401
    response = client.post("/api/v1/auth/login",json={"username":"test-validator","password":password})
    assert response.status_code == 429 and response.json()["code"] == "rate_limited"
    clock[0] += timedelta(seconds=60)
    login(client,password)


def test_logout_invalidates_the_session_and_requires_csrf(tmp_path):
    client, _, password, _ = validator_fixture(tmp_path)
    session = login(client,password).json()
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert client.get("/api/v1/auth/session").json()["authenticated"]
    assert client.post("/api/v1/auth/logout",headers={"X-CSRF-Token":session["csrf_token"]}).status_code == 204
    assert not client.get("/api/v1/auth/session").json()["authenticated"]


def test_ambiguous_legacy_case_variants_fail_closed(tmp_path):
    client, factory, password, _ = validator_fixture(tmp_path)
    with factory.begin() as db:
        account = dict(db.execute(sa.select(CONTENT["validators"])).mappings().one())
        account.pop("id")
        account["username"] = "Test-Validator"
        db.execute(CONTENT["validators"].insert().values(**account))
    response = client.post("/api/v1/auth/login",json={"username":"test-validator","password":password})
    assert response.status_code == 401 and response.headers["cache-control"] == "private, no-store"
    assert not client.get("/api/v1/auth/session").json()["authenticated"]
