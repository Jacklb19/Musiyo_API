import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.contracts import (
    CatalogFacet,
    CatalogPage,
    ResourceAccess,
    SelectionCleared,
    SelectionConfirmed,
)
from app.main import Element, Tour, app

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def test_resource_access_contract_is_canonical_and_versioned():
    schema = read_json(CONTRACTS / "resource-access.v1.schema.json")
    assert schema == ResourceAccess.model_json_schema()
    example = read_json(CONTRACTS / "examples/resource_access.json")
    Draft202012Validator(schema).validate(example)
    ResourceAccess.model_validate_json(json.dumps(example))
    with pytest.raises(ValidationError):
        ResourceAccess.model_validate_json(json.dumps(example | {"schema_version": True}))


def test_catalog_contracts_and_example_are_canonical():
    schema = read_json(CONTRACTS / "catalog.v1.schema.json")
    assert schema == CatalogPage.model_json_schema()
    assert read_json(CONTRACTS / "catalog-facet.v1.schema.json") == CatalogFacet.model_json_schema()
    example = read_json(CONTRACTS / "examples/catalog.json")
    Draft202012Validator(schema).validate(example)
    CatalogPage.model_validate(example)
    for mutation in ({"schema_version": True}, {"total": 0}, {"limit": 101}, {"items": example["items"] * 2}):
        with pytest.raises(ValidationError):
            CatalogPage.model_validate(example | mutation)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_saved_openapi_matches_application():
    assert read_json(CONTRACTS / "openapi.json") == app.openapi()


def test_saved_tour_schema_matches_model_and_examples():
    schema = read_json(CONTRACTS / "tour.v1.schema.json")
    assert schema == Tour.model_json_schema()
    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    for name in ("tour_full.json", "tour_empty_point.json"):
        example = read_json(CONTRACTS / "examples" / name)
        validator.validate(example)
        assert Tour.model_validate_json(json.dumps(example)).model_dump(mode="json") == example
    future = read_json(CONTRACTS / "examples" / "tour_version_2.json")
    assert list(validator.iter_errors(future))
    try:
        Tour.model_validate(future)
    except ValidationError:
        pass
    else:
        raise AssertionError("A future tour version was accepted")


def test_element_example_matches_public_model():
    example = read_json(CONTRACTS / "examples" / "element.json")
    schema = read_json(CONTRACTS / "element.v1.schema.json")
    assert schema == Element.model_json_schema()
    Draft202012Validator(schema).validate(example)
    Element.model_validate_json(json.dumps(example))


def test_bridge_example_matches_versioned_schema():
    example = read_json(CONTRACTS / "examples" / "selection_confirmed.json")
    schema = read_json(CONTRACTS / "bridge.v1.schema.json")
    assert schema == SelectionConfirmed.model_json_schema()
    Draft202012Validator(schema).validate(example)
    SelectionConfirmed.model_validate(example)


def test_cleared_selection_is_versioned_and_has_no_stale_element():
    example = read_json(CONTRACTS / "examples" / "selection_cleared.json")
    schema = read_json(CONTRACTS / "bridge-clear.v1.schema.json")
    assert schema == SelectionCleared.model_json_schema()
    Draft202012Validator(schema).validate(example)
    SelectionCleared.model_validate(example)
    example["data"]["element_slug"] = "stale"
    with pytest.raises(ValidationError):
        SelectionCleared.model_validate(example)
    example["data"].pop("element_slug")
    example["version"] = True
    with pytest.raises(ValidationError):
        SelectionCleared.model_validate(example)


@pytest.mark.parametrize("mutation", ["duplicate_point", "duplicate_element", "unknown_activation", "wrong_type", "boolean_version", "unknown_field"])
def test_invalid_tours_are_rejected(mutation):
    example = read_json(CONTRACTS / "examples" / "tour_full.json")
    points = example["rooms"][0]["points"]
    if mutation == "duplicate_point":
        points[1]["key"] = points[0]["key"]
    elif mutation == "duplicate_element":
        points[0]["elements"].append(points[0]["elements"][0])
    elif mutation == "unknown_activation":
        points[0]["activation"] = ["touch"]
    elif mutation == "wrong_type":
        example["schema_version"] = "1"
    elif mutation == "boolean_version":
        example["schema_version"] = True
    else:
        example["rooms"][0]["unexpected"] = True
    with pytest.raises(ValidationError):
        Tour.model_validate(example)
