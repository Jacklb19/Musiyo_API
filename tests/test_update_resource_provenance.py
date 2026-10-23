import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_guide import guide as guide

from app.content_tables import CONTENT
from app.main import create_app
from scripts.local_backup import backup
from scripts.update_credits import matching_backup
from scripts.update_resource_provenance import ProvenancePlan, apply

DECISION = dict(approved_by="Synthetic owner", approved_on="2026-10-23", reference="synthetic-provenance")


def plan(*changes):
    return ProvenancePlan.model_validate(dict(operation_version=1, decision=DECISION, changes=list(changes)))


def element(guide):
    return TestClient(create_app(guide[1])).get("/api/v1/elements/synthetic-import").json()


def revision(guide):
    return TestClient(create_app(guide[1])).get("/api/v1/tours/recorrido-prueba").json()["tour"]["revision"]


def paradata(guide, resource_id="narration-test"):
    with guide[1]() as db:
        return db.scalar(sa.select(CONTENT["resources"].c.paradata).where(CONTENT["resources"].c.id == resource_id))


CHANGE = dict(resource_id="narration-test", expected="Synthetic silence", provenance="Narración del museo (2026).")


def test_preview_then_guarded_apply_changes_only_the_public_provenance(guide, tmp_path):
    database = Path(guide[0].removeprefix("sqlite:///"))
    before, tour_before = element(guide), revision(guide)
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    preview = apply(database, plan(CHANGE), dry_run=True)
    assert not preview["applied"] and preview["changes"][0]["after"] == "Narración del museo (2026)."
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    backup(database, tmp_path / "private", tmp_path / "snapshot")
    matching_backup(database, tmp_path / "snapshot" / "backup-manifest.json")
    result = apply(database, plan(CHANGE), dry_run=False)
    assert result["applied"] and set(result["tour_revisions"]) == {"recorrido-prueba"}
    after = element(guide)
    resource = next(item for item in after["resources"] if item["id"] == "narration-test")
    assert resource["provenance"] == "Narración del museo (2026)."
    assert resource["credit"] == "Test generator"
    assert {key: value for key, value in after.items() if key != "resources"} == \
        {key: value for key, value in before.items() if key != "resources"}
    assert paradata(guide)["internal_notes"][0]["text"] == "Synthetic silence"
    assert revision(guide) != tour_before
    with pytest.raises(ValueError, match="differs"):
        apply(database, plan(CHANGE), dry_run=False)


def test_provenance_can_be_cleared_and_keeps_every_earlier_note(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    apply(database, plan(CHANGE), dry_run=False)
    apply(database, plan(dict(resource_id="narration-test", expected="Narración del museo (2026).", provenance=None)), dry_run=False)
    resource = next(item for item in element(guide)["resources"] if item["id"] == "narration-test")
    assert resource["provenance"] is None
    assert [note["text"] for note in paradata(guide)["internal_notes"]] == ["Synthetic silence", "Narración del museo (2026)."]


def test_unknown_resources_blank_text_and_repeats_are_refused(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    with pytest.raises(ValueError, match="Unknown resource"):
        apply(database, plan(CHANGE | dict(resource_id="no-existe")), dry_run=False)
    with pytest.raises(ValidationError):
        plan(CHANGE | dict(provenance="   "))
    with pytest.raises(ValidationError):
        plan(CHANGE, CHANGE)
    with pytest.raises(ValidationError):
        plan(CHANGE | dict(provenance="Synthetic silence"))
