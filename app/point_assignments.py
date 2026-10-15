"""Compare-and-set point assignments used only by explicit private imports."""
import sqlalchemy as sa
from pydantic import Field, model_validator

from .contracts import ContractModel, Identifier
from .db import PointElementRecord, PointRecord, RoomRecord, TourRecord
from .public_repository import PublicRepository


class PointChange(ContractModel):
    point_key: Identifier
    expected: list[Identifier]
    replacement: list[Identifier]

    @model_validator(mode="after")
    def unique_elements(self):
        if len(set(self.expected)) != len(self.expected) or len(set(self.replacement)) != len(self.replacement):
            raise ValueError("Repeated point element")
        return self


class PointAssignments(ContractModel):
    changes: list[PointChange] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_points(self):
        if len({item.point_key for item in self.changes}) != len(self.changes):
            raise ValueError("Repeated point change")
        return self


def validate_assignments(db, plan: PointAssignments):
    for change in plan.changes:
        if db.get(PointRecord, change.point_key) is None:
            raise ValueError("Unknown assignment point")
        current = list(db.scalars(sa.select(PointElementRecord.element_id).where(
            PointElementRecord.point_id == change.point_key).order_by(PointElementRecord.order)))
        if current != change.expected:
            raise ValueError("Point assignments changed since review")


def apply_assignments(db, plan: PointAssignments, now):
    repository = PublicRepository(db)
    keys = set()
    for change in plan.changes:
        if any(repository.element(slug) is None for slug in change.replacement):
            raise ValueError("Assignment requires currently published elements")
        db.execute(sa.delete(PointElementRecord).where(PointElementRecord.point_id == change.point_key))
        for order, slug in enumerate(change.replacement):
            db.add(PointElementRecord(point_id=change.point_key, element_id=slug, order=order))
        keys.add(db.scalar(sa.select(RoomRecord.tour_id).join(PointRecord, PointRecord.room_id == RoomRecord.id)
            .where(PointRecord.id == change.point_key)))
    for key in keys:
        tour = db.get(TourRecord, key)
        tour.revision = now
