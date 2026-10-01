import hashlib
import json
import secrets
import wave
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.cli import import_test_data, main
from app.content_operations import (
    create_validator,
    export_state,
    import_package,
    reindex,
    revoke,
    verify_validity,
    withdraw_tour,
)
from app.content_tables import CONTENT
from app.db import make_session_factory
from app.main import create_app
from app.migrations import upgrade_database


def package_fixture(tmp_path):
    url = f"sqlite:///{tmp_path / 'content.db'}"
    upgrade_database(url)
    import_test_data(url)
    audio = tmp_path / "test.wav"
    with wave.open(str(audio), "wb") as writer:
        writer.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        writer.writeframes(b'\x00\x00' * 8000)
    value = {"manifest_version": 1,
        "decision": {"reference": "TEST-DECISION", "responsible": "Synthetic role", "decision_date": "2026-09-27",
            "approved_culturally": True, "authorized_from": "2026-09-27T00:00:00Z"},
        "elements": [{"kind": "other", "point_keys": ["punto-01", "punto-02"], "record": {
            "slug": "synthetic-import", "title": "[CONTENIDO DE PRUEBA — NO CULTURAL]", "description": "Synthetic description",
            "blocks": [{"id": "block-test", "kind": "interpretation", "text": "Synthetic text", "context": "Synthetic context"}],
            "resources": [{"id": "narration-test", "kind": "narration", "mime": "audio/wav", "duration_seconds": 1.0,
                "transcription": "TEST TRANSCRIPT", "credit": "Test generator", "provenance": "Synthetic silence"}]}}],
        "files": {"narration-test": {"file": "test.wav", "sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}}}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return url, manifest, value


def test_dry_run_has_no_content_or_storage_side_effects(tmp_path):
    url, manifest, value = package_fixture(tmp_path)
    storage = tmp_path / "private"
    assert import_package(manifest, url, dry_run=True, private_storage=storage)["dry_run"]
    assert not storage.exists()
    state = export_state(url)
    assert state["elements"] == [] and state["counts"]["authorizations"] == 0 and state["counts"]["sources"] == 0


def test_import_repeat_and_revocation_are_visible_immediately(tmp_path):
    url, manifest, value = package_fixture(tmp_path)
    storage = tmp_path / "private"
    import_package(manifest, url, private_storage=storage)
    import_package(manifest, url, private_storage=storage)
    client = TestClient(create_app(make_session_factory(url), private_storage=storage))
    record = client.get("/api/v1/elements/synthetic-import").json()
    assert record["resources"][0]["transcription"] == "TEST TRANSCRIPT" and len(record["blocks"]) == 1
    assert client.get("/api/v1/elements/synthetic-import/resources/narration-test").status_code == 200
    points = client.get("/api/v1/tours/recorrido-prueba").json()["rooms"][0]["points"]
    assert points[0]["elements"][0]["slug"] == points[1]["elements"][0]["slug"] == "synthetic-import"
    assert reindex(url)["indexed_fragments"] == 1
    assert reindex(url)["indexed_fragments"] == 1
    assert revoke("synthetic-import", database_url=url, reason="test_withdrawal")["revoked"] == 1
    assert client.get("/api/v1/elements/synthetic-import").status_code == 404
    assert client.get("/api/v1/elements/synthetic-import/resources/narration-test").status_code == 404
    assert export_state(url)["counts"]["index_fragments"] == 0


def test_resource_revoke_and_tour_withdrawal(tmp_path):
    url, manifest, value = package_fixture(tmp_path)
    import_package(manifest, url, private_storage=tmp_path / "private")
    assert revoke("narration-test", resource=True, database_url=url)["revoked"] == 1
    client = TestClient(create_app(make_session_factory(url)))
    assert client.get("/api/v1/elements/synthetic-import").json()["resources"] == []
    withdraw_tour("recorrido-prueba", url)
    assert client.get("/api/v1/tours/recorrido-prueba").status_code == 404


@pytest.mark.parametrize("error", ["unknown_point", "hash", "outside", "version", "interpretation", "missing_credit", "header"])
def test_invalid_packages_cannot_create_public_content(tmp_path, error):
    url, manifest, value = package_fixture(tmp_path)
    if error == "unknown_point":
        value["elements"][0]["point_keys"] = ["unknown"]
    elif error == "hash":
        value["files"]["narration-test"]["sha256"] = '0' * 64
    elif error == "outside":
        value["files"]["narration-test"]["file"] = '../test.wav'
    elif error == "version":
        value["manifest_version"] = True
    elif error == "interpretation":
        value["elements"][0]["record"]["blocks"][0]["context"] = " "
    elif error == "missing_credit":
        value["elements"][0]["record"]["resources"][0]["credit"] = None
    else:
        (tmp_path / 'test.wav').write_bytes(b'INVALID RESOURCE')
        value["files"]["narration-test"]["sha256"] = hashlib.sha256(b'INVALID RESOURCE').hexdigest()
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises((ValueError, ValidationError)):
        import_package(manifest, url, private_storage=tmp_path / "private")
    assert export_state(url)["counts"]["elements"] == 0
    assert not (tmp_path / "private").exists()


def test_verify_validity_removes_expired_index_entries(tmp_path):
    url, manifest, value = package_fixture(tmp_path)
    import_package(manifest, url, private_storage=tmp_path / "private")
    reindex(url)
    with make_session_factory(url).begin() as db:
        db.execute(CONTENT["authorizations"].update().where(CONTENT["authorizations"].c.element_id == "synthetic-import")
            .values(authorized_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert verify_validity(url)["removed_fragments"] == 1


def test_validator_secret_is_hashed_and_export_excludes_it(tmp_path):
    url, manifest, value = package_fixture(tmp_path)
    password = secrets.token_urlsafe(24)
    create_validator("test-validator", "Synthetic validator", password, url)
    with make_session_factory(url)() as db:
        stored = db.scalar(sa.select(CONTENT["validators"].c.password_hash))
    assert stored.startswith('$argon2id$') and PasswordHasher().verify(stored, password)
    assert password not in json.dumps(export_state(url)) and stored not in json.dumps(export_state(url))
    with pytest.raises(ValueError):
        create_validator("test-validator", "Synthetic validator", password, url)


def test_cli_commands_operate_on_an_isolated_database(tmp_path, monkeypatch, capsys):
    url, manifest, value = package_fixture(tmp_path)
    monkeypatch.setenv("MUSIYO_DATABASE_URL", url)
    monkeypatch.setenv("MUSIYO_STORAGE_ROOT", str(tmp_path / "private"))
    main(["import", str(manifest), "--dry-run"])
    assert json.loads(capsys.readouterr().out)["dry_run"]
    main(["import", str(manifest)])
    capsys.readouterr()
    main(["reindex"])
    assert json.loads(capsys.readouterr().out)["indexed_fragments"] == 1
    main(["verify-validity"])
    capsys.readouterr()
    output = tmp_path / "state.json"
    main(["export-state", "--output", str(output)])
    capsys.readouterr()
    assert json.loads(output.read_text(encoding="utf-8"))["elements"] == ["synthetic-import"]
    password = secrets.token_urlsafe(24)
    monkeypatch.setattr("app.cli.getpass.getpass", lambda prompt: password)
    main(["create-validator", "--username", "test", "--name", "Synthetic role"])
    assert "validator_id" in json.loads(capsys.readouterr().out)
    main(["revoke", "--element", "synthetic-import", "--reason", "test"])
    assert json.loads(capsys.readouterr().out)["revoked"] == 1
    main(["withdraw", "--tour", "recorrido-prueba"])
    assert json.loads(capsys.readouterr().out)["withdrawn_tour"] == "recorrido-prueba"
