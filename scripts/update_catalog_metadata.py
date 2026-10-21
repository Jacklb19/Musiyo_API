"""Reviewed catalogue metadata corrections: categories and materials (compare-and-set).

The external plan may create categories (new slugs only) and, per element, set a category and/or
materials after checking the values it currently has. --dry-run prints the difference. Applying
requires a matching scripts.local_backup folder and one transaction that refuses any change to
authorizations, restrictions, sources, blocks, details, credits, resources or assignments.
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.content_tables import CONTENT
from app.db import make_session_factory
from scripts.evaluate_guide import external_file
from scripts.local_backup import MANIFEST
from scripts.update_credits import Decision, DryRun, matching_backup

CATEGORIES, METADATA = CONTENT["categories"], CONTENT["element_metadata"]
GUARDED = ("authorizations", "restrictions", "sources", "detail_blocks", "elements", "element_details",
           "credits", "resources", "point_elements", "points", "collection_elements")
SLUG = r"^[a-z0-9][a-z0-9-]{1,99}$"
UNSET = "__unset__"


class NewCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=2, max_length=80)
    order: int = Field(default=0, ge=0, le=1000)


class ElementMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=1, max_length=200)
    expected_category: str | None = UNSET
    category: str | None = Field(default=UNSET, pattern=SLUG)
    expected_materials: list[str] | None = None
    materials: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def paired(self):
        if (self.category == UNSET) != (self.expected_category == UNSET):
            raise ValueError("A category change needs its expected current category")
        if (self.materials is None) != (self.expected_materials is None):
            raise ValueError("A materials change needs the expected current materials")
        if self.category == UNSET and self.materials is None:
            raise ValueError("Nothing to change")
        if self.materials is not None and any(not item.strip() or len(item) > 80 for item in self.materials):
            raise ValueError("Invalid material")
        return self


class MetadataPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_version: int
    decision: Decision
    categories: list[NewCategory] = Field(default_factory=list, max_length=10)
    elements: list[ElementMetadata] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique(self):
        if self.operation_version != 1:
            raise ValueError("Unknown metadata operation version")
        if len({item.slug for item in self.elements}) != len(self.elements) or \
                len({item.slug for item in self.categories}) != len(self.categories):
            raise ValueError("Repeated element or category in plan")
        return self


def snapshot(db, touched: set[str]):
    tables = {name: [tuple(row) for row in db.execute(sa.select(CONTENT[name]).order_by(*CONTENT[name].primary_key.columns))]
              for name in GUARDED}
    others = [tuple(row) for row in db.execute(sa.select(METADATA).where(METADATA.c.element_id.not_in(touched))
                                               .order_by(METADATA.c.element_id))]
    fixed = [tuple(row) for row in db.execute(sa.select(METADATA.c.element_id, METADATA.c.community_id, METADATA.c.kind,
                                                        METADATA.c.technique, METADATA.c.highlighted)
                                              .where(METADATA.c.element_id.in_(touched)).order_by(METADATA.c.element_id))]
    return tables, others, fixed


def apply(database: Path, plan: MetadataPlan, *, dry_run: bool) -> dict:
    factory = make_session_factory("sqlite:///" + database.as_posix())
    changes: list[dict] = []
    touched = {item.slug for item in plan.elements}
    try:
        with factory.begin() as db:
            before = snapshot(db, touched)
            for category in plan.categories:
                if db.scalar(sa.select(CATEGORIES.c.id).where(CATEGORIES.c.slug == category.slug)):
                    raise ValueError(f"Category {category.slug} already exists")
                changes.append(dict(target=category.slug, field="new_category", before=None, after=category.name))
                if not dry_run:
                    db.execute(CATEGORIES.insert().values(id=str(uuid.uuid4()), slug=category.slug,
                                                          name=category.name, order=category.order))
            planned = {category.slug for category in plan.categories}
            for item in plan.elements:
                row = db.execute(sa.select(METADATA.c.category_id, METADATA.c.materials)
                                 .where(METADATA.c.element_id == item.slug)).first()
                if row is None:
                    raise ValueError(f"{item.slug}: no catalogue metadata")
                current_slug = db.scalar(sa.select(CATEGORIES.c.slug).where(CATEGORIES.c.id == row.category_id)) \
                    if row.category_id else None
                values = {}
                if item.category != UNSET:
                    if current_slug != item.expected_category:
                        raise ValueError(f"{item.slug}: current category differs from the plan")
                    target = db.scalar(sa.select(CATEGORIES.c.id).where(CATEGORIES.c.slug == item.category))
                    if target is None and not (dry_run and item.category in planned):
                        raise ValueError(f"Unknown category {item.category}")
                    values["category_id"] = target
                    changes.append(dict(target=item.slug, field="category", before=current_slug, after=item.category))
                if item.materials is not None:
                    current = list(row.materials or [])
                    if current != item.expected_materials:
                        raise ValueError(f"{item.slug}: current materials differ from the plan")
                    values["materials"] = item.materials
                    changes.append(dict(target=item.slug, field="materials", before=current, after=item.materials))
                if not dry_run:
                    db.execute(METADATA.update().where(METADATA.c.element_id == item.slug).values(**values))
            if dry_run:
                raise DryRun()
            if snapshot(db, touched) != before:
                raise ValueError("Guarded content changed; transaction rolled back")
    except DryRun:
        pass
    finally:
        factory.kw["bind"].dispose()
    return dict(schema_version=1, applied=not dry_run, database=str(database), decision=plan.decision.model_dump(mode="json"),
                changes=changes, unchanged=", ".join(GUARDED) + ", other element metadata")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("--database", required=True)
    parser.add_argument("--backup-folder", help=f"Folder holding {MANIFEST} for this exact database state")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    plan = MetadataPlan.model_validate_json(external_file(arguments.plan).read_text(encoding="utf-8-sig"))
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
