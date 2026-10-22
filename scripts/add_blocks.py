"""Append reviewed detail blocks to published records (compare-and-set on the current block count).

Each plan entry names an element, the number of blocks it has now and the blocks to append (kind, text,
an existing source and, for testimony, an attribution). A cited source not yet linked to the element is
linked. --dry-run prints the change. Applying requires a matching scripts.local_backup folder and one
transaction in which nothing else changes; the guide index of touched records is rebuilt and the
revision of tours that show them is bumped so clients refresh.
"""
import argparse
import json
import sys
from pathlib import Path

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.content_operations import reindex_in_session
from app.content_tables import CONTENT
from app.db import (
    PointElementRecord,
    PointRecord,
    RoomRecord,
    TourRecord,
    make_session_factory,
    utc_now,
)
from scripts.correct_texts import rows
from scripts.evaluate_guide import external_file
from scripts.local_backup import MANIFEST
from scripts.update_credits import Decision, DryRun, matching_backup

BLOCKS, SOURCES, LINKS = CONTENT["detail_blocks"], CONTENT["sources"], CONTENT["element_sources"]


class NewBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern=r"^(documented_fact|testimony|interpretation)$")
    text: str = Field(min_length=20, max_length=2000)
    source_id: str = Field(min_length=1, max_length=100)
    attribution: str | None = Field(default=None, max_length=500)
    context: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def shape(self):
        if self.kind == "testimony" and not (self.attribution or "").strip():
            raise ValueError("A testimony needs its attribution")
        return self


class ElementBlocks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=1, max_length=200)
    expected_block_count: int = Field(ge=0)
    blocks: list[NewBlock] = Field(min_length=1, max_length=10)


class BlockPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_version: int = Field(ge=1, le=1)
    decision: Decision
    elements: list[ElementBlocks] = Field(min_length=1, max_length=30)


def apply(database: Path, plan: BlockPlan, *, dry_run: bool, now=utc_now) -> dict:
    factory = make_session_factory("sqlite:///" + database.as_posix())
    added, revisions = [], {}
    touched = sorted({item.slug for item in plan.elements})
    try:
        with factory.begin() as db:
            before = rows(db)
            for item in plan.elements:
                current = db.scalar(sa.select(sa.func.count()).select_from(BLOCKS).where(BLOCKS.c.element_id == item.slug))
                if current != item.expected_block_count:
                    raise ValueError(f"{item.slug}: has {current} blocks, the plan expects {item.expected_block_count}")
                linked = set(db.scalars(sa.select(LINKS.c.source_id).where(LINKS.c.element_id == item.slug)))
                for offset, block in enumerate(item.blocks):
                    if db.scalar(sa.select(SOURCES.c.id).where(SOURCES.c.id == block.source_id)) is None:
                        raise ValueError(f"Unknown source {block.source_id}")
                    if block.source_id not in linked:
                        if not dry_run:
                            db.execute(LINKS.insert().values(element_id=item.slug, source_id=block.source_id))
                        linked.add(block.source_id)
                    if not dry_run:
                        db.execute(BLOCKS.insert().values(element_id=item.slug, order=current + offset, **block.model_dump()))
                    added.append(dict(slug=item.slug, order=current + offset, kind=block.kind, source_id=block.source_id,
                                      text=block.text))
            if dry_run:
                raise DryRun()
            for slug in touched:
                reindex_in_session(db, slug)
            stamp = now()
            keys = set(db.scalars(sa.select(RoomRecord.tour_id).join(PointRecord, PointRecord.room_id == RoomRecord.id)
                                  .join(PointElementRecord, PointElementRecord.point_id == PointRecord.id)
                                  .where(PointElementRecord.element_id.in_(touched))))
            for key in sorted(keys):
                tour = db.get(TourRecord, key)
                tour.revision = stamp
                revisions[key] = stamp.isoformat()
            db.flush()
            after = rows(db)
            for name in before:
                if name in ("detail_blocks", "element_sources"):
                    if not set(before[name]) <= set(after[name]) or any(after[name][key] != value for key, value in before[name].items()):
                        raise ValueError(f"Existing {name} changed; transaction rolled back")
                    extra = [row for key, row in after[name].items() if key not in before[name]]
                    if any(row["element_id"] not in touched for row in extra):
                        raise ValueError(f"Unplanned {name} rows; transaction rolled back")
                elif after[name] != before[name]:
                    raise ValueError(f"{name} changed outside the plan; transaction rolled back")
    except DryRun:
        pass
    finally:
        factory.kw["bind"].dispose()
    return dict(schema_version=1, applied=not dry_run, decision=plan.decision.model_dump(mode="json"),
                added=added, tour_revisions=revisions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("--database", required=True)
    parser.add_argument("--backup-folder", help=f"Folder holding {MANIFEST} for this exact database state")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    plan = BlockPlan.model_validate_json(external_file(arguments.plan).read_text(encoding="utf-8-sig"))
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
