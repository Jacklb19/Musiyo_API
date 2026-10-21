import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_guide import guide as guide

from app.main import create_app
from scripts.local_backup import backup
from scripts.update_catalog_metadata import MetadataPlan, apply

DECISION = dict(approved_by="Synthetic owner", approved_on="2026-10-20", reference="synthetic")


def plan(expected_category, expected_materials, **extra):
    return MetadataPlan.model_validate(dict(operation_version=1, decision=DECISION,
        categories=[dict(slug="celebraciones", name="Celebraciones", order=6)],
        elements=[dict(slug="synthetic-import", expected_category=expected_category, category="celebraciones",
                       expected_materials=expected_materials, materials=["madera"])], **extra))


def current(guide):
    body = TestClient(create_app(guide[1])).get("/api/v1/elements/synthetic-import").json()
    return (body["category"] or {}).get("slug"), body["materials"]


def test_new_category_and_materials_with_preview_and_guarded_apply(guide, tmp_path):
    database = Path(guide[0].removeprefix("sqlite:///"))
    category, materials = current(guide)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    preview = apply(database, plan(category, materials), dry_run=True)
    assert not preview["applied"] and [item["field"] for item in preview["changes"]] == ["new_category", "category", "materials"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    backup(database, tmp_path / "private", tmp_path / "snapshot")
    assert apply(database, plan(category, materials), dry_run=False)["applied"]
    assert current(guide) == ("celebraciones", ["madera"])
    facets = TestClient(create_app(guide[1])).get("/api/v1/categories").json()
    assert {"slug": "celebraciones", "name": "Celebraciones", "element_count": 1}.items() <= next(
        item for item in facets if item["slug"] == "celebraciones").items()
    found = TestClient(create_app(guide[1])).get("/api/v1/catalog", params={"query": "Celebraciones"}).json()
    assert [item["slug"] for item in found["items"]] == ["synthetic-import"]
    with pytest.raises(ValueError):
        apply(database, plan(category, materials), dry_run=False)


def test_stale_values_and_incomplete_changes_are_refused(guide):
    category, materials = current(guide)
    database = Path(guide[0].removeprefix("sqlite:///"))
    with pytest.raises(ValueError):
        apply(database, plan("otra", materials), dry_run=False)
    with pytest.raises(ValueError):
        apply(database, plan(category, ["cuero"]), dry_run=False)
    assert current(guide) == (category, materials)
    with pytest.raises(ValidationError):
        MetadataPlan.model_validate(dict(operation_version=1, decision=DECISION,
                                         elements=[dict(slug="synthetic-import", category="celebraciones")]))
