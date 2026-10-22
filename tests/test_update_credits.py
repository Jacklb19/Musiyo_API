import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa
from test_guide import guide as guide

from app.content_tables import CONTENT
from app.guide_repository import index_status
from scripts.local_backup import backup
from scripts.update_credits import CreditPlan, apply, matching_backup


def plan(expected, resource_credit):
    return CreditPlan.model_validate(dict(operation_version=1,
        decision=dict(approved_by="Synthetic owner", approved_on="2026-10-20", reference="synthetic-review"),
        elements=[dict(slug="synthetic-import", expected_credits=expected,
                       credits=[dict(role="Pertenencia", name="Colectiva sintética"), dict(role="Creador", name="Desconocido")],
                       resources=[dict(id="narration-test", expected_credit=resource_credit, credit="Captura sintética")])]))


def current(factory):
    with factory() as db:
        credits = db.execute(sa.select(CONTENT["credits"].c.role, CONTENT["credits"].c.name)
                             .where(CONTENT["credits"].c.element_id == "synthetic-import", CONTENT["credits"].c.resource_id.is_(None))
                             .order_by(CONTENT["credits"].c.order)).all()
        resource = db.scalar(sa.select(CONTENT["resources"].c.credit).where(CONTENT["resources"].c.id == "narration-test"))
        grants = db.execute(sa.select(CONTENT["authorizations"]).order_by(CONTENT["authorizations"].c.id)).all()
    return [tuple(row) for row in credits], resource, grants


def test_dry_run_changes_nothing_and_apply_changes_only_credits(guide, tmp_path):
    database = Path(guide[0].removeprefix("sqlite:///"))
    credits, resource, grants = current(guide[1])
    expected = [dict(role=role, name=name) for role, name in credits]
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    preview = apply(database, plan(expected, resource), dry_run=True)
    assert not preview["applied"] and len(preview["changes"]) == 2
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    backup(database, tmp_path / "private", tmp_path / "snapshot")
    matching_backup(database, tmp_path / "snapshot" / "backup-manifest.json")
    assert apply(database, plan(expected, resource), dry_run=False)["applied"]
    after_credits, after_resource, after_grants = current(guide[1])
    assert after_credits == [("Pertenencia", "Colectiva sintética"), ("Creador", "Desconocido")]
    assert after_resource == "Captura sintética" and after_grants == grants
    with guide[1]() as db:
        # Credits are part of each fragment's fingerprint: the guide must still find this ficha.
        assert index_status(db) == []
    with pytest.raises(ValueError):
        matching_backup(database, tmp_path / "snapshot" / "backup-manifest.json")


def test_stale_expectations_and_foreign_resources_are_refused(guide):
    database = Path(guide[0].removeprefix("sqlite:///"))
    credits, resource, _ = current(guide[1])
    expected = [dict(role=role, name=name) for role, name in credits]
    with pytest.raises(ValueError):
        apply(database, plan(expected + [dict(name="Otro")], resource), dry_run=False)
    with pytest.raises(ValueError):
        apply(database, plan(expected, "different credit"), dry_run=False)
    assert current(guide[1])[0] == credits
