import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_guide import guide as guide
from test_guide import question

from app.main import create_app
from scripts.correct_texts import TextPlan, apply

DECISION = dict(approved_by="Synthetic owner", approved_on="2026-10-21", reference="synthetic")


def plan(*changes):
    return TextPlan.model_validate(dict(operation_version=1, decision=DECISION, changes=list(changes)))


DESCRIPTION = dict(slug="synthetic-import", field="description", expected="sin términos", replacement="con términos corregidos")
BLOCK = dict(slug="synthetic-import", field="block", block_order=0, expected="produce sonido", replacement="emite un sonido grave")


def record(guide):
    return TestClient(create_app(guide[1])).get("/api/v1/elements/synthetic-import").json()


def test_preview_then_guarded_apply_changes_only_the_named_fragments(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    before_record = record(guide)
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    preview = apply(database, plan(DESCRIPTION, BLOCK), dry_run=True)
    assert not preview["applied"] and {item["table"] for item in preview["changes"]} >= {"element_details", "detail_blocks"}
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    result = apply(database, plan(DESCRIPTION, BLOCK), dry_run=False)
    assert result["applied"]
    after = record(guide)
    assert after["description"] == "Descripción sintética con términos corregidos de búsqueda."
    assert after["blocks"][0]["text"] == "El instrumento sintético emite un sonido grave durante la prueba."
    assert {key: value for key, value in after.items() if key not in {"description", "blocks"}} == \
        {key: value for key, value in before_record.items() if key not in {"description", "blocks"}}
    found = TestClient(create_app(guide[1])).get("/api/v1/catalog", params={"query": "corregidos"}).json()
    assert [item["slug"] for item in found["items"]] == ["synthetic-import"]
    answer = guide[2].ask(question("¿Qué sonido emite el instrumento?"))
    assert "sonido grave" in answer.answer
    with pytest.raises(ValueError):
        apply(database, plan(DESCRIPTION), dry_run=False)


def test_stale_or_ambiguous_fragments_are_refused(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    before = record(guide)
    with pytest.raises(ValueError):
        apply(database, plan(DESCRIPTION | dict(expected="texto que no existe")), dry_run=False)
    with pytest.raises(ValueError):
        apply(database, plan(BLOCK | dict(block_order=7)), dry_run=False)
    with pytest.raises(ValueError):
        apply(database, plan(DESCRIPTION, BLOCK | dict(expected="ru")), dry_run=False)
    assert record(guide) == before
    with pytest.raises(ValidationError):
        plan(dict(slug="synthetic-import", field="block", expected="produce sonido", replacement="x"))
    with pytest.raises(ValidationError):
        plan(dict(slug="synthetic-import", field="block_context", expected="contexto", replacement="x"))


def test_block_context_and_attribution_are_corrected_in_place(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    import sqlite3
    with sqlite3.connect(database) as db:
        db.execute("UPDATE detail_blocks SET context='Nota interna de las secciones 7.3', attribution='Autora citada (2020)' WHERE element_id='synthetic-import'")
    apply(database, plan(dict(slug="synthetic-import", field="block_context", block_order=0, expected="Nota interna de las secciones 7.3", replacement="Nota para visitantes"),
                         dict(slug="synthetic-import", field="block_attribution", block_order=0, expected=" (2020)", replacement="")), dry_run=False)
    block = record(guide)["blocks"][0]
    assert block["context"] == "Nota para visitantes" and block["attribution"] == "Autora citada"
    assert block["text"] == "El instrumento sintético produce sonido durante la prueba."


def test_source_reference_is_corrected_only_through_an_element_that_cites_it(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    change = dict(slug="synthetic-import", field="source_reference", source_id="test-source",
                  expected="Referencia sintética de prueba", replacement="Referencia corregida")
    apply(database, plan(change), dry_run=False)
    assert record(guide)["sources"][0]["reference"] == "Referencia corregida"
    with pytest.raises(ValueError):
        apply(database, plan(change | dict(source_id="otra-fuente", expected="Referencia corregida", replacement="x y z")), dry_run=False)
    with pytest.raises(ValidationError):
        plan(dict(slug="synthetic-import", field="source_reference", expected="Referencia", replacement="x"))
