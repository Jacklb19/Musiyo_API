"""Reviewed short descriptions of tour rooms (compare-and-set of the whole text).

Each change names one tour, one room, the exact text the room shows today and its replacement.
--dry-run prints the difference without writing. Applying requires a matching backup
folder and runs in one transaction in which every other row and column stays identical; the
revision of the touched tours is bumped so clients refresh. The target is the local SQLite museum
(--database) or the online PostgreSQL database (--database-url); see scripts.correction_target.
"""
import argparse
import json
import sys
from pathlib import Path

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.content_tables import CONTENT
from app.db import Base, RoomRecord, TourRecord, utc_now
from scripts import correction_target
from scripts.correct_texts import DERIVED
from scripts.evaluate_guide import external_file
from scripts.update_credits import Decision, DryRun


class RoomChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tour: str = Field(min_length=1, max_length=100)
    room: str = Field(min_length=1, max_length=100)
    expected: str = Field(max_length=2000)
    replacement: str = Field(min_length=3, max_length=600)

    @model_validator(mode="after")
    def changed(self):
        if self.expected == self.replacement:
            raise ValueError("Nothing to change")
        return self


class RoomPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_version: int
    decision: Decision
    changes: list[RoomChange] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique(self):
        if self.operation_version != 1:
            raise ValueError("Unknown room text operation version")
        keys = [(item.tour, item.room) for item in self.changes]
        if len(set(keys)) != len(keys):
            raise ValueError("Repeated room in plan")
        return self


def snapshot(db) -> dict:
    """Every row this operation may read, keyed by primary key; tour revisions are compared apart."""
    tables = [CONTENT[name] for name in CONTENT if name not in DERIVED]
    tables += [Base.metadata.tables[name] for name in ("rooms", "points", "point_elements")]
    state = {table.name: {tuple(row[column.name] for column in table.primary_key.columns): dict(row)
                          for row in db.execute(sa.select(table)).mappings()} for table in tables}
    state["tours"] = {row.id: {column: getattr(row, column) for column in ("name", "published", "guide_key", "guide_name",
                                                                           "guide_room_key", "guide_available")}
                      for row in db.scalars(sa.select(TourRecord))}
    return state


def apply(database: Path | str, plan: RoomPlan, *, dry_run: bool, now=utc_now) -> dict:
    factory = correction_target.session_factory(database)
    changes, revisions = [], {}
    try:
        with factory.begin() as db:
            before = snapshot(db)
            expected = {name: {key: dict(value) for key, value in table.items()} for name, table in before.items()}
            for change in plan.changes:
                room = db.get(RoomRecord, change.room)
                if room is None or room.tour_id != change.tour:
                    raise ValueError(f"{change.room}: not a room of {change.tour}")
                if room.short_description != change.expected:
                    raise ValueError(f"{change.room}: the current text differs from the plan")
                expected["rooms"][(change.room,)]["short_description"] = change.replacement
                changes.append(dict(tour=change.tour, room=change.room, name=room.name,
                                    before=change.expected, after=change.replacement))
            if dry_run:
                raise DryRun()
            for change in plan.changes:
                db.get(RoomRecord, change.room).short_description = change.replacement
            stamp = now()
            for key in sorted({change.tour for change in plan.changes}):
                db.get(TourRecord, key).revision = stamp
                revisions[key] = stamp.isoformat()
            db.flush()
            if snapshot(db) != expected:
                raise ValueError("Content outside the planned room texts changed; transaction rolled back")
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
    plan = RoomPlan.model_validate_json(external_file(arguments.plan).read_text(encoding="utf-8-sig"))
    target = correction_target.selected(arguments)
    correction_target.require_backup(arguments, target)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(json.dumps(apply(target, plan, dry_run=arguments.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
