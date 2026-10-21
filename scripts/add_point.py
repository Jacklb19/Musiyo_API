"""Add a reviewed tour point holding currently published fichas (compare-and-set).

Used when Unity has committed a matching anchor ("ANCLA <key> LISTA"). The point key must be new,
the room must exist, every element must be published and not already shown at another point
(unless the plan says so). One transaction updates the tour revision and refuses any other
change. --dry-run previews; applying requires a matching scripts.local_backup folder.
"""
import argparse
import json
import sys
from pathlib import Path

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.content_tables import CONTENT
from app.db import (
    PointElementRecord,
    PointRecord,
    RoomRecord,
    TourRecord,
    make_session_factory,
    utc_now,
)
from app.public_repository import PublicRepository
from scripts.evaluate_guide import external_file
from scripts.local_backup import MANIFEST
from scripts.update_credits import Decision, DryRun, matching_backup

KEY = r"^[a-z0-9][a-z0-9._-]{2,99}$"
GUARDED = ("authorizations", "restrictions", "sources", "detail_blocks", "elements", "element_details",
           "element_metadata", "credits", "resources", "categories", "rooms")


class PointPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_version: int
    decision: Decision
    unity_anchor: str = Field(min_length=3, max_length=300, description="Unity commit and coordination line")
    room_key: str = Field(pattern=KEY)
    point_key: str = Field(pattern=KEY)
    name: str = Field(min_length=1, max_length=250)
    activation: list[str] = Field(min_length=1, max_length=4)
    elements: list[str] = Field(min_length=1, max_length=8)
    allow_elements_elsewhere: bool = False

    @model_validator(mode="after")
    def valid(self):
        if self.operation_version != 1:
            raise ValueError("Unknown point operation version")
        if not set(self.activation) <= {"proximity", "gaze", "keyboard"} or len(set(self.activation)) != len(self.activation):
            raise ValueError("Unknown or repeated activation")
        if len(set(self.elements)) != len(self.elements):
            raise ValueError("Repeated element")
        return self


def guarded(db):
    return {name: [tuple(row) for row in db.execute(sa.select(CONTENT[name]).order_by(*CONTENT[name].primary_key.columns))]
            for name in GUARDED}, \
        [tuple(row) for row in db.execute(sa.select(CONTENT["points"]).order_by(CONTENT["points"].c.id))], \
        [tuple(row) for row in db.execute(sa.select(CONTENT["point_elements"]).order_by(CONTENT["point_elements"].c.point_id,
                                                                                         CONTENT["point_elements"].c.element_id))]


def apply(database: Path, plan: PointPlan, *, dry_run: bool, now=utc_now) -> dict:
    factory = make_session_factory("sqlite:///" + database.as_posix())
    result: dict = {}
    try:
        with factory.begin() as db:
            before_tables, before_points, before_links = guarded(db)
            room = db.get(RoomRecord, plan.room_key)
            if room is None:
                raise ValueError("Unknown room")
            if db.get(PointRecord, plan.point_key) is not None:
                raise ValueError("The point already exists")
            public = PublicRepository(db, now)
            if any(public.element(slug) is None for slug in plan.elements):
                raise ValueError("Every element must be currently published")
            shown = set(db.scalars(sa.select(PointElementRecord.element_id).where(PointElementRecord.element_id.in_(plan.elements))))
            if shown and not plan.allow_elements_elsewhere:
                raise ValueError(f"Already shown at another point: {sorted(shown)}")
            order = (db.scalar(sa.select(sa.func.max(PointRecord.order)).where(PointRecord.room_id == plan.room_key)) or 0) + 1
            result = dict(room=plan.room_key, point=plan.point_key, order=order, activation=plan.activation, elements=plan.elements)
            if dry_run:
                raise DryRun()
            db.add(PointRecord(id=plan.point_key, room_id=plan.room_key, order=order, name=plan.name, activation=plan.activation))
            db.flush()
            for index, slug in enumerate(plan.elements):
                db.add(PointElementRecord(point_id=plan.point_key, element_id=slug, order=index))
            tour = db.get(TourRecord, room.tour_id)
            tour.revision = now()
            db.flush()
            after_tables, after_points, after_links = guarded(db)
            new_points = [row for row in after_points if row[0] != plan.point_key]
            new_links = [row for row in after_links if row[0] != plan.point_key]
            if after_tables != before_tables or new_points != before_points or new_links != before_links:
                raise ValueError("Guarded content changed; transaction rolled back")
            result["tour_revision"] = tour.revision.isoformat()
    except DryRun:
        pass
    finally:
        factory.kw["bind"].dispose()
    return dict(schema_version=1, applied=not dry_run, decision=plan.decision.model_dump(mode="json"),
                unity_anchor=plan.unity_anchor, **result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("--database", required=True)
    parser.add_argument("--backup-folder")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    plan = PointPlan.model_validate_json(external_file(arguments.plan).read_text(encoding="utf-8-sig"))
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
