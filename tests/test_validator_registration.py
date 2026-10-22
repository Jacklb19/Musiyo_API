import secrets

import sqlalchemy as sa
from argon2 import PasswordHasher

from app.content_tables import CONTENT
from tests.test_validator import validator_fixture

CODE = "invitacion-" + secrets.token_hex(8)
PASSWORD = "contraseña-segura-" + secrets.token_hex(4)


def account(**extra):
    return {"username": "Nueva.Validadora", "name": "Nueva  Validadora", "password": PASSWORD, "invitation_code": CODE} | extra


def invite(monkeypatch, tmp_path, code=CODE):
    path = tmp_path / "invitation.txt"
    path.write_text(code + "\n", encoding="utf-8")
    monkeypatch.setenv("MUSIYO_VALIDATOR_INVITATION_FILE", str(path))


def test_registration_is_closed_without_a_server_invitation_code(tmp_path, monkeypatch):
    client, factory, _, _ = validator_fixture(tmp_path)
    monkeypatch.delenv("MUSIYO_VALIDATOR_INVITATION_FILE", raising=False)
    assert client.get("/api/v1/auth/registration").json() == {"schema_version": 1, "enabled": False}
    assert client.post("/api/v1/auth/register", json=account()).status_code == 403
    invite(monkeypatch, tmp_path, code="corto")
    assert client.get("/api/v1/auth/registration").json()["enabled"] is False
    with factory() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CONTENT["validators"])) == 1


def test_invited_person_registers_signs_in_and_can_log_in_again(tmp_path, monkeypatch):
    client, factory, _, _ = validator_fixture(tmp_path)
    invite(monkeypatch, tmp_path)
    assert client.get("/api/v1/auth/registration").json()["enabled"] is True
    response = client.post("/api/v1/auth/register", json=account())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authenticated"] and body["user"] == {"username": "nueva.validadora", "name": "Nueva Validadora"}
    assert "secure" in response.headers["set-cookie"].lower() and PASSWORD not in response.text and CODE not in response.text
    assert client.get("/api/v1/auth/session").json()["authenticated"]
    with factory() as db:
        stored = db.execute(sa.select(CONTENT["validators"]).where(CONTENT["validators"].c.username == "nueva.validadora")).mappings().one()
        assert stored["active"] and PasswordHasher().verify(stored["password_hash"], PASSWORD)
    client.cookies.clear()
    login = client.post("/api/v1/auth/login", json={"username": "nueva.validadora", "password": PASSWORD})
    assert login.status_code == 200
    assert client.post("/api/v1/auth/register", json=account()).status_code == 409


def test_wrong_code_weak_password_and_cross_origin_are_refused(tmp_path, monkeypatch):
    client, factory, _, _ = validator_fixture(tmp_path)
    invite(monkeypatch, tmp_path)
    assert client.post("/api/v1/auth/register", json=account(invitation_code="otro-codigo-cualquiera")).status_code == 401
    assert client.post("/api/v1/auth/register", json=account(password="corta")).status_code == 422
    assert client.post("/api/v1/auth/register", json=account(username="no válido")).status_code == 422
    assert client.post("/api/v1/auth/register", json=account(), headers={"Origin": "https://otro.example"}).status_code == 403
    with factory() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(CONTENT["validators"])) == 1


def test_registration_attempts_share_the_login_rate_limit(tmp_path, monkeypatch):
    client, _, _, _ = validator_fixture(tmp_path)
    invite(monkeypatch, tmp_path)
    statuses = [client.post("/api/v1/auth/register", json=account(invitation_code=f"incorrecto-{n:04d}")).status_code for n in range(11)]
    assert statuses[:10] == [401] * 10 and statuses[10] == 429
    assert client.post("/api/v1/auth/register", json=account()).status_code == 429
