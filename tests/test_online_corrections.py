"""The correction tools and the online backup against a real PostgreSQL database.

Each test gets its own musiyo_verify_* database next to MUSIYO_TEST_POSTGRES_URL, filled from the
same synthetic package as the SQLite tests, and drops it afterwards.
"""
import json
import os
import shutil
import subprocess
from argparse import ArgumentParser
from uuid import uuid4

import pytest
import sqlalchemy as sa
import test_add_blocks as blocks
import test_correct_texts as texts
import test_update_credits as credits
import test_update_room_texts as rooms
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from test_guide import guide as guide
from test_guide import question

from app.cli import import_package, import_test_data
from app.db import make_session_factory
from app.guide_service import GuideService
from app.main import create_app
from app.migrations import upgrade_database
from scripts import (
    add_blocks,
    correct_texts,
    correction_target,
    online_backup,
    update_credits,
    update_room_texts,
)

BASE = os.getenv("MUSIYO_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not BASE, reason="Dedicated PostgreSQL URL required")


def admin(statement):
    engine = sa.create_engine(BASE, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as db:
            db.execute(sa.text(statement))
    finally:
        engine.dispose()


@pytest.fixture
def online(guide, tmp_path):
    base = make_url(BASE)
    assert (base.database or "").startswith("musiyo_verify_"), "Only isolated verification databases are allowed"
    names = [f"{base.database}_{uuid4().hex[:8]}", f"{base.database}_{uuid4().hex[:8]}"]
    for name in names:
        admin(f'CREATE DATABASE "{name}"')
    url, spare = (base.set(database=name).render_as_string(hide_password=False) for name in names)
    factory = make_session_factory(url)
    try:
        upgrade_database(url)
        import_test_data(url)
        import_package(guide[3], url, private_storage=tmp_path / "pg-private")
        yield url, factory, spare
    finally:
        factory.kw["bind"].dispose()
        for name in names:
            admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def snapshot(url, folder):
    """A manifest of the current state, as scripts.online_backup writes it (without the dump)."""
    folder.mkdir()
    (folder / online_backup.MANIFEST).write_text(json.dumps(dict(schema_version=1, kind=online_backup.KIND,
        database=online_backup.state(url)), default=str), encoding="utf-8")
    return folder


def arguments(*values):
    parser = ArgumentParser()
    correction_target.add_arguments(parser)
    return parser.parse_args(list(values))


def element(factory, slug="synthetic-import"):
    return TestClient(create_app(factory)).get(f"/api/v1/elements/{slug}").json()


def revision(factory):
    return TestClient(create_app(factory)).get("/api/v1/tours/recorrido-prueba").json()["tour"]["revision"]


def test_text_correction_needs_a_matching_online_backup_and_changes_only_its_fragments(online, tmp_path):
    url, factory, _ = online
    before, tour_before = element(factory), revision(factory)
    state = online_backup.state(url)
    preview = correct_texts.apply(url, texts.plan(texts.DESCRIPTION, texts.BLOCK), dry_run=True)
    assert not preview["applied"] and online_backup.state(url) == state
    assert "***" in preview["database"] and make_url(url).password not in preview["database"]
    with pytest.raises(ValueError, match="online_backup"):
        correction_target.require_backup(arguments("--database-url", url), url)
    folder = snapshot(url, tmp_path / "backup")
    correction_target.require_backup(arguments("--database-url", url, "--backup-folder", str(folder)), url)
    result = correct_texts.apply(url, texts.plan(texts.DESCRIPTION, texts.BLOCK), dry_run=False)
    assert result["applied"] and set(result["tour_revisions"]) == {"recorrido-prueba"}
    after = element(factory)
    assert after["description"] == "Descripción sintética con términos corregidos de búsqueda."
    assert after["blocks"][0]["text"] == "El instrumento sintético emite un sonido grave durante la prueba."
    assert {key: value for key, value in after.items() if key not in {"description", "blocks"}} == \
        {key: value for key, value in before.items() if key not in {"description", "blocks"}}
    assert revision(factory) != tour_before
    found = TestClient(create_app(factory)).get("/api/v1/catalog", params={"query": "corregidos"}).json()
    assert [item["slug"] for item in found["items"]] == ["synthetic-import"]
    assert "sonido grave" in GuideService(factory).ask(question("¿Qué sonido emite el instrumento?")).answer
    # The backup now describes an older state: the next correction needs a new one.
    with pytest.raises(ValueError, match="make a new backup"):
        correction_target.require_backup(arguments("--database-url", url, "--backup-folder", str(folder)), url)
    with pytest.raises(ValueError):
        correct_texts.apply(url, texts.plan(texts.DESCRIPTION), dry_run=False)


def test_a_validator_signing_in_does_not_invalidate_the_backup(online, tmp_path):
    url, factory, _ = online
    folder = snapshot(url, tmp_path / "backup")
    with factory.begin() as db:
        db.execute(sa.text("INSERT INTO validator_login_attempts (id, ip_hash, occurred_at) "
                           "VALUES ('attempt-1', :hash, now())"), dict(hash="0" * 64))
    online_backup.matching_online_backup(url, folder / online_backup.MANIFEST)


def test_blocks_credits_and_room_texts_apply_online(online):
    url, factory, _ = online
    count = len(element(factory)["blocks"])
    added = add_blocks.apply(url, blocks.plan(count, blocks.FACT, blocks.TESTIMONY), dry_run=False)
    assert added["applied"] and [item["order"] for item in added["added"]] == [count, count + 1]
    assert [block["kind"] for block in element(factory)["blocks"]][-2:] == ["documented_fact", "testimony"]
    assert "tallo vaciado" in GuideService(factory).ask(question("¿Con qué se fabrica el instrumento sintético?")).answer

    current, resource, grants = credits.current(factory)
    expected = [dict(role=role, name=name) for role, name in current]
    tour_before = revision(factory)
    result = update_credits.apply(url, credits.plan(expected, resource), dry_run=False)
    assert result["applied"] and set(result["tour_revisions"]) == {"recorrido-prueba"}
    after_credits, after_resource, after_grants = credits.current(factory)
    assert after_credits == [("Pertenencia", "Colectiva sintética"), ("Creador", "Desconocido")]
    assert after_resource == "Captura sintética" and after_grants == grants and revision(factory) != tour_before

    holder = [None, factory]
    room = rooms.change(holder)
    assert update_room_texts.apply(url, rooms.plan(room), dry_run=False)["applied"]
    assert rooms.tour(holder)["rooms"][0]["short_description"] == room["replacement"]
    with pytest.raises(ValueError, match="differs"):
        update_room_texts.apply(url, rooms.plan(room), dry_run=False)


def test_online_transactions_read_one_snapshot(online):
    url, _, _ = online
    factory = correction_target.session_factory(url)
    try:
        with factory() as db:
            assert db.connection().get_isolation_level() == "REPEATABLE READ"
    finally:
        factory.kw["bind"].dispose()


def test_database_url_without_a_value_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(correction_target.ENVIRONMENT, BASE)
    assert correction_target.selected(arguments("--database-url")) == BASE
    with pytest.raises(ValueError, match="SQLite"):
        correction_target.selected(arguments("--database-url", "sqlite:///museum.db"))


@pytest.mark.skipif(shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
                    reason="Docker runs pg_dump when it is not installed")
def test_backup_restores_into_an_empty_database_only(online, tmp_path):
    url, factory, spare = online
    manifest = online_backup.backup(url, tmp_path / "weekly")
    assert manifest["dump"]["bytes"] > 0 and manifest["database"]["revision"] == "0005"
    assert make_url(url).password not in (tmp_path / "weekly" / online_backup.MANIFEST).read_text(encoding="utf-8")
    report = online_backup.restore(tmp_path / "weekly", spare)
    assert report["passed"] and report["differing_tables"] == []
    assert element(make_session_factory(spare)) == element(factory)
    with pytest.raises(ValueError, match="empty database"):
        online_backup.restore(tmp_path / "weekly", url)
    (tmp_path / "weekly" / online_backup.DUMP).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="does not match"):
        online_backup.restore(tmp_path / "weekly", spare)
