"""Reviewed wording corrections in published records (compare-and-set substring replacement).

Each change names one element and either its description or one detail block (by order: its text,
context or attribution), the exact current fragment and its replacement; the fragment must occur exactly once. The description
is changed in element_details and, where it holds the same fragment, in elements. --dry-run prints
the difference. Applying requires a matching backup folder and one transaction in
which every other row and column stays identical; the guide index of touched records is rebuilt
and the revision of tours that show them is bumped so clients refresh. The target is the local
SQLite museum (--database) or the online PostgreSQL database (--database-url); see
scripts.correction_target.
"""
import argparse
import json
import sys
from pathlib import Path

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.content_operations import reindex_in_session
from app.content_tables import CONTENT
from app.db import utc_now
from scripts import correction_target
from scripts.evaluate_guide import external_file
from scripts.update_credits import Decision, DryRun

# Derived tables are rebuilt by triggers (element_search) or by reindex (index_fragments).
DERIVED = ("index_fragments", "element_search", "tours", "alembic_version")
TEXT_TABLES = {"element_details": ("description",), "elements": ("description",),
               "detail_blocks": ("text", "context", "attribution"), "sources": ("reference",)}
BLOCK_COLUMNS = {"block": "text", "block_context": "context", "block_attribution": "attribution"}


class TextChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=1, max_length=200)
    field: str = Field(pattern=r"^(description|block|block_context|block_attribution|source_reference)$")
    block_order: int | None = Field(default=None, ge=0)
    source_id: str | None = Field(default=None, min_length=1, max_length=100)
    expected: str = Field(min_length=3, max_length=2000)
    replacement: str = Field(max_length=2000)

    @model_validator(mode="after")
    def shape(self):
        if (self.field in BLOCK_COLUMNS) != (self.block_order is not None):
            raise ValueError("A block change needs block_order; a description change must not have one")
        if (self.field == "source_reference") != (self.source_id is not None):
            raise ValueError("A source change needs source_id (of a source the named element cites)")
        if self.expected == self.replacement:
            raise ValueError("Nothing to change")
        return self


class TextPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_version: int
    decision: Decision
    changes: list[TextChange] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def version(self):
        if self.operation_version != 1:
            raise ValueError("Unknown text operation version")
        return self


def rows(db):
    names = [name for name in CONTENT if name not in DERIVED]
    return {name: {tuple(row[column.name] for column in CONTENT[name].primary_key.columns): dict(row)
                   for row in db.execute(sa.select(CONTENT[name])).mappings()} for name in names}


def replaced(text: str | None, change: TextChange, where: str) -> str:
    if text is None or text.count(change.expected) != 1:
        raise ValueError(f"{where}: the expected fragment must occur exactly once")
    return text.replace(change.expected, change.replacement)


def apply(database: Path | str, plan: TextPlan, *, dry_run: bool, now=utc_now) -> dict:
    factory = correction_target.session_factory(database)
    changes, revisions = [], {}
    try:
        with factory.begin() as db:
            before = rows(db)
            expected = {name: {key: dict(value) for key, value in table.items()} for name, table in before.items()}
            for change in plan.changes:
                targets = []
                if change.field == "description":
                    targets.append(("element_details", (change.slug,), "description"))
                    element = expected["elements"].get((change.slug,))
                    if element and element["description"] and change.expected in element["description"]:
                        targets.append(("elements", (change.slug,), "description"))
                elif change.field == "source_reference":
                    if (change.slug, change.source_id) not in expected["element_sources"]:
                        raise ValueError(f"{change.slug}: does not cite {change.source_id}")
                    targets.append(("sources", (str(change.source_id),), "reference"))
                else:
                    block = [key for key, row in expected["detail_blocks"].items()
                             if row["element_id"] == change.slug and row["order"] == change.block_order]
                    targets.extend(("detail_blocks", key, BLOCK_COLUMNS[change.field]) for key in block)
                if not targets or targets[0][1] not in expected[targets[0][0]]:
                    raise ValueError(f"{change.slug}: no {change.field} to correct")
                for table, key, column in targets:
                    old = expected[table][key][column]
                    expected[table][key][column] = replaced(old, change, f"{change.slug} {change.field}")
                    changes.append(dict(target=change.slug, table=table, field=change.field, block_order=change.block_order,
                                        before=change.expected, after=change.replacement))
            cited = {change.source_id for change in plan.changes if change.source_id}
            touched = sorted({change.slug for change in plan.changes}
                             | {element for element, source in expected["element_sources"] if source in cited})
            if dry_run:
                raise DryRun()
            for table, columns in TEXT_TABLES.items():
                for key, row in expected[table].items():
                    values = {column: row[column] for column in columns if row[column] != before[table][key][column]}
                    if values:
                        primary = CONTENT[table].primary_key.columns
                        db.execute(CONTENT[table].update().where(*[c == v for c, v in zip(primary, key)]).values(values))
            for slug in touched:
                reindex_in_session(db, slug)
            revisions = correction_target.bump_tours(db, touched, now())
            db.flush()
            after = rows(db)
            for table, columns in TEXT_TABLES.items():
                for key, row in expected[table].items():
                    # The corrected rows get a fresh updated_at from the table's onupdate default.
                    if any(row[column] != before[table][key][column] for column in columns) and "updated_at" in row:
                        row["updated_at"] = after[table][key]["updated_at"]
            if after != expected:
                raise ValueError("Content outside the planned wording changed; transaction rolled back")
    except DryRun:
        pass
    finally:
        factory.kw["bind"].dispose()
    return dict(schema_version=1, applied=not dry_run, database=correction_target.label(database), decision=plan.decision.model_dump(mode="json"),
                changes=changes, tour_revisions=revisions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    correction_target.add_arguments(parser)
    arguments = parser.parse_args()
    plan = TextPlan.model_validate_json(external_file(arguments.plan).read_text(encoding="utf-8-sig"))
    target = correction_target.selected(arguments)
    correction_target.require_backup(arguments, target)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(json.dumps(apply(target, plan, dry_run=arguments.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
