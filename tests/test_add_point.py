import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_guide import guide as guide

from app.main import create_app
from scripts.add_point import PointPlan, apply

DECISION = dict(approved_by="Synthetic owner", approved_on="2026-10-20", reference="synthetic")


def tour(guide):
    return TestClient(create_app(guide[1])).get("/api/v1/tours/recorrido-prueba").json()


def plan(guide, **extra):
    room = tour(guide)["rooms"][0]["key"]
    return PointPlan.model_validate(dict(operation_version=1, decision=DECISION, unity_anchor="synthetic commit",
                                         room_key=room, point_key="punto.prueba.nuevo", name="Punto sintético",
                                         activation=["proximity", "keyboard"], elements=["synthetic-import"]) | extra)


def test_new_point_is_previewed_then_added_with_a_new_tour_revision(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="Already shown"):
        apply(database, plan(guide), dry_run=False)
    preview = apply(database, plan(guide, allow_elements_elsewhere=True), dry_run=True)
    assert not preview["applied"] and preview["point"] == "punto.prueba.nuevo"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    old_revision = tour(guide)["tour"].get("revision")
    result = apply(database, plan(guide, allow_elements_elsewhere=True), dry_run=False)
    points = {point["key"]: point for room in tour(guide)["rooms"] for point in room["points"]}
    assert [item["slug"] for item in points["punto.prueba.nuevo"]["elements"]] == ["synthetic-import"]
    assert result["tour_revision"] and tour(guide)["tour"].get("revision") != old_revision
    with pytest.raises(ValueError, match="already exists"):
        apply(database, plan(guide, allow_elements_elsewhere=True), dry_run=False)


def test_unknown_room_unpublished_element_and_bad_activation_are_refused(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    with pytest.raises(ValueError):
        apply(database, plan(guide, room_key="room.missing"), dry_run=False)
    with pytest.raises(ValueError):
        apply(database, plan(guide, elements=["missing-element"]), dry_run=False)
    with pytest.raises(ValidationError):
        plan(guide, activation=["teleport"])
