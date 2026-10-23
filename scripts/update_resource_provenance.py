"""Reviewed public provenance of published resources (compare-and-set of the whole text).

Each change names one resource, the exact provenance it shows today and the reviewed public text,
or null to show none. The text it replaces is not lost: it is appended to the resource's internal
paradata under "internal_notes", which is never published. --dry-run prints the difference without
writing. Applying requires a matching backup folder and runs in one transaction in which every
other row and column stays identical; the revision of tours that show the touched fichas is bumped
so clients refresh. Provenance is not part of the guide index, so no reindex is needed. The target
is the local SQLite museum (--database) or the online PostgreSQL database (--database-url); see
scripts.correction_target.
"""
import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.content_tables import CONTENT
from app.db import utc_now
from scripts import correction_target
from scripts.evaluate_guide import external_file
from scripts.update_credits import Decision, DryRun
from scripts.update_room_texts import snapshot

RESOURCES = CONTENT["resources"]


class ProvenanceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_id: str = Field(min_length=1, max_length=200)
    expected: str | None = Field(max_length=5000)
    provenance: str | None = Field(default=None, max_length=600)

    @model_validator(mode="after")
    def changed(self):
        if self.expected == self.provenance:
            raise ValueError("Nothing to change")
        if self.provenance is not None and not self.provenance.strip():
            raise ValueError("Use null, not blank text, to show no provenance")
        return self


class ProvenancePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_version: int
    decision: Decision
    changes: list[ProvenanceChange] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique(self):
        if self.operation_version != 1:
            raise ValueError("Unknown provenance operation version")
        ids = [item.resource_id for item in self.changes]
        if len(set(ids)) != len(ids):
            raise ValueError("Repeated resource in plan")
        return self


def apply(database: Path | str, plan: ProvenancePlan, *, dry_run: bool, now=utc_now) -> dict:
    factory = correction_target.session_factory(database)
    changes, revisions = [], {}
    stamp = now()
    try:
        with factory.begin() as db:
            before = snapshot(db)
            expected = {name: {key: dict(value) for key, value in table.items()} for name, table in before.items()}
            resources = expected["resources"]
            touched = set()
            for change in plan.changes:
                row = resources.get((change.resource_id,))
                if row is None:
                    raise ValueError(f"Unknown resource {change.resource_id}")
                if row["provenance"] != change.expected:
                    raise ValueError(f"{change.resource_id}: the current provenance differs from the plan")
                paradata = dict(row["paradata"] or {})
                notes = list(paradata.get("internal_notes", []))
                if change.expected:
                    notes.append({"moved_on": stamp.date().isoformat(), "field": "provenance", "text": change.expected,
                                  "reference": plan.decision.reference})
                paradata["internal_notes"] = notes
                row["provenance"], row["paradata"] = change.provenance, paradata
                if row["element_id"]:
                    touched.add(row["element_id"])
                changes.append(dict(resource=change.resource_id, element=row["element_id"], kind=row["kind"],
                                    before=change.expected, after=change.provenance))
            if dry_run:
                raise DryRun()
            for change in plan.changes:
                row = resources[(change.resource_id,)]
                db.execute(RESOURCES.update().where(RESOURCES.c.id == change.resource_id)
                           .values(provenance=row["provenance"], paradata=row["paradata"]))
            revisions = correction_target.bump_tours(db, touched, stamp)
            db.flush()
            after = snapshot(db)
            for change in plan.changes:
                # The touched rows get a fresh updated_at from the table's onupdate default.
                key = (change.resource_id,)
                if "updated_at" in resources[key]:
                    resources[key]["updated_at"] = after["resources"][key]["updated_at"]
            if after != expected:
                raise ValueError("Content outside the planned provenance changed; transaction rolled back")
    except DryRun:
        pass
    finally:
        factory.kw["bind"].dispose()
    return dict(schema_version=1, applied=not dry_run, database=correction_target.label(database),
                decision=plan.decision.model_dump(mode="json"), changes=changes, tour_revisions=revisions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    correction_target.add_arguments(parser)
    arguments = parser.parse_args()
    plan = ProvenancePlan.model_validate_json(external_file(arguments.plan).read_text(encoding="utf-8-sig"))
    target = correction_target.selected(arguments)
    correction_target.require_backup(arguments, target)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(json.dumps(apply(target, plan, dry_run=arguments.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
