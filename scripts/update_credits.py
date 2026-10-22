"""Reviewed credit corrections for published fichas and their resources (compare-and-set).

The external plan names each element, the credits it currently shows, the reviewed replacement
and, per resource, the expected and new credit text. --dry-run prints the difference without
writing. Applying requires a backup manifest from scripts.local_backup that matches the selected
database, runs in one transaction and refuses if authorizations, restrictions, sources or any
other element change. Credits are display metadata: no grant or publication changes. They are
part of each guide fragment's fingerprint, so the guide index of touched fichas is rebuilt.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.content_operations import reindex_in_session
from app.content_tables import CONTENT
from app.db import make_session_factory
from scripts.evaluate_guide import external_file
from scripts.local_backup import MANIFEST, summary

CREDITS, RESOURCES, AUTHORIZATIONS = CONTENT["credits"], CONTENT["resources"], CONTENT["authorizations"]


class Credit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=500)


class ResourceCredit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=200)
    expected_credit: str | None
    credit: str = Field(min_length=1, max_length=1000)


class ElementCredits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=1, max_length=200)
    expected_credits: list[Credit]
    credits: list[Credit] = Field(min_length=1, max_length=20)
    resources: list[ResourceCredit] = Field(default_factory=list, max_length=40)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_by: str = Field(min_length=1, max_length=200)
    approved_on: date
    reference: str = Field(min_length=1, max_length=500)


class CreditPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_version: int
    decision: Decision
    elements: list[ElementCredits] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique(self):
        if self.operation_version != 1:
            raise ValueError("Unknown credit operation version")
        slugs = [item.slug for item in self.elements]
        resources = [resource.id for item in self.elements for resource in item.resources]
        if len(set(slugs)) != len(slugs) or len(set(resources)) != len(resources):
            raise ValueError("Repeated element or resource in plan")
        return self


def element_credits(db, slug):
    rows = db.execute(sa.select(CREDITS.c.role, CREDITS.c.name).where(CREDITS.c.element_id == slug, CREDITS.c.resource_id.is_(None))
                      .order_by(CREDITS.c.order, CREDITS.c.id)).all()
    return [Credit(role=row.role, name=row.name) for row in rows]


def guarded_state(db, plan: CreditPlan):
    """Everything this operation must not change, read inside the same transaction."""
    touched = {item.slug for item in plan.elements}
    tables = {name: [tuple(row) for row in db.execute(sa.select(CONTENT[name]).order_by(*CONTENT[name].primary_key.columns))]
              for name in ("authorizations", "restrictions", "sources", "detail_blocks", "elements", "element_details")}
    others = [tuple(row) for row in db.execute(sa.select(CREDITS).where(sa.or_(CREDITS.c.element_id.is_(None),
              CREDITS.c.element_id.not_in(touched), CREDITS.c.resource_id.is_not(None))).order_by(CREDITS.c.id))]
    resources = [tuple(row) for row in db.execute(sa.select(*[column for column in RESOURCES.c if column.name not in {"credit", "updated_at"}])
                 .order_by(RESOURCES.c.id))]
    return tables, others, resources


class DryRun(Exception):
    """Leaves the transaction through rollback after the comparison."""


def apply(database: Path, plan: CreditPlan, *, dry_run: bool) -> dict:
    factory = make_session_factory("sqlite:///" + database.as_posix())
    changes = []
    try:
        with factory.begin() as db:
            before = guarded_state(db, plan)
            for item in plan.elements:
                current = element_credits(db, item.slug)
                if db.scalar(sa.select(sa.func.count()).select_from(CONTENT["elements"]).where(CONTENT["elements"].c.id == item.slug)) != 1:
                    raise ValueError(f"Unknown element {item.slug}")
                if current != item.expected_credits:
                    raise ValueError(f"{item.slug}: current credits differ from the plan")
                changes.append(dict(target=item.slug, field="credits", before=[c.model_dump() for c in current],
                                    after=[c.model_dump() for c in item.credits]))
                for resource in item.resources:
                    row = db.execute(sa.select(RESOURCES.c.element_id, RESOURCES.c.credit).where(RESOURCES.c.id == resource.id)).first()
                    if row is None or row.element_id != item.slug:
                        raise ValueError(f"{resource.id}: not a resource of {item.slug}")
                    if row.credit != resource.expected_credit:
                        raise ValueError(f"{resource.id}: current credit differs from the plan")
                    changes.append(dict(target=resource.id, field="credit", before=row.credit, after=resource.credit))
                if not dry_run:
                    db.execute(CREDITS.delete().where(CREDITS.c.element_id == item.slug, CREDITS.c.resource_id.is_(None)))
                    db.execute(CREDITS.insert(), [dict(element_id=item.slug, resource_id=None, role=credit.role, name=credit.name, order=index)
                                                  for index, credit in enumerate(item.credits)])
                    for resource in item.resources:
                        db.execute(RESOURCES.update().where(RESOURCES.c.id == resource.id).values(credit=resource.credit))
            if not dry_run:
                if guarded_state(db, plan) != before:
                    raise ValueError("Guarded content changed; transaction rolled back")
                for item in plan.elements:
                    if element_credits(db, item.slug) != item.credits:
                        raise ValueError("Credits were not stored as planned")
                    # Stale fragments would be dropped at retrieval and the guide would abstain.
                    reindex_in_session(db, item.slug)
            else:
                raise DryRun()
    except DryRun:
        pass
    finally:
        factory.kw["bind"].dispose()
    return dict(schema_version=1, applied=not dry_run, database=str(database), decision=plan.decision.model_dump(mode="json"),
                changes=changes, unchanged="authorizations, restrictions, sources, blocks, elements, details, other credits and resource fields")


def matching_backup(database: Path, manifest_path: Path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    current = summary(database)
    recorded = manifest["database"]
    if (recorded["revision"], recorded["row_counts"], recorded["revoked_authorizations"]) != (
            current["revision"], current["row_counts"], current["revoked_authorizations"]):
        raise ValueError("The backup does not match the selected database's current state")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("--database", required=True)
    parser.add_argument("--backup-folder", help=f"Folder holding {MANIFEST} for this exact database state")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    plan = CreditPlan.model_validate_json(external_file(arguments.plan).read_text(encoding="utf-8-sig"))
    database = external_file(arguments.database)
    if not arguments.dry_run:
        if not arguments.backup_folder:
            raise ValueError("Applying requires --backup-folder from scripts.local_backup")
        matching_backup(database, Path(arguments.backup_folder).resolve(strict=True) / MANIFEST)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(json.dumps(apply(database, plan, dry_run=arguments.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
