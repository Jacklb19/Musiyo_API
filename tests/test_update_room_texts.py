import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_guide import guide as guide

from app.main import create_app
from scripts.local_backup import backup
from scripts.update_credits import matching_backup
from scripts.update_room_texts import RoomPlan, apply

DECISION = dict(approved_by="Synthetic owner", approved_on="2026-10-22", reference="synthetic")


def tour(guide):
    return TestClient(create_app(guide[1])).get("/api/v1/tours/recorrido-prueba").json()


def plan(*changes):
    return RoomPlan.model_validate(dict(operation_version=1, decision=DECISION, changes=list(changes)))


def change(guide, replacement="Sala sintética con una ficha de instrumentos.", **overrides):
    room = tour(guide)["rooms"][0]
    return dict(tour="recorrido-prueba", room=room["key"], expected=room["short_description"], replacement=replacement) | overrides


def test_preview_then_guarded_apply_changes_only_the_room_text(guide, tmp_path):
    database = Path(guide[0].removeprefix("sqlite:///"))
    before = tour(guide)
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    preview = apply(database, plan(change(guide)), dry_run=True)
    assert not preview["applied"] and preview["changes"][0]["after"] == "Sala sintética con una ficha de instrumentos."
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    backup(database, tmp_path / "private", tmp_path / "snapshot")
    matching_backup(database, tmp_path / "snapshot" / "backup-manifest.json")
    result = apply(database, plan(change(guide)), dry_run=False)
    assert result["applied"] and set(result["tour_revisions"]) == {"recorrido-prueba"}
    after = tour(guide)
    assert after["rooms"][0]["short_description"] == "Sala sintética con una ficha de instrumentos."
    assert after["tour"]["revision"] != before["tour"]["revision"]
    assert after["rooms"][0]["points"] == before["rooms"][0]["points"] and after["rooms"][1:] == before["rooms"][1:]


def test_stale_or_foreign_rooms_are_refused(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    before = tour(guide)
    with pytest.raises(ValueError):
        apply(database, plan(change(guide, expected="Texto que ya no está")), dry_run=False)
    with pytest.raises(ValueError):
        apply(database, plan(change(guide, tour="otro-recorrido")), dry_run=False)
    with pytest.raises(ValueError):
        apply(database, plan(change(guide, room="sala-inexistente")), dry_run=False)
    assert tour(guide) == before
    with pytest.raises(ValidationError):
        plan(change(guide), change(guide))
    with pytest.raises(ValidationError):
        plan(change(guide, replacement=before["rooms"][0]["short_description"] or "igual", expected=before["rooms"][0]["short_description"] or "igual"))
