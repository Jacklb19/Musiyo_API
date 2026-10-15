"""Revoke only a reviewed addition; never restore grants from an old snapshot."""
from pathlib import Path

import sqlalchemy as sa

from .content_package import ContentPackage
from .content_tables import CONTENT
from .db import (
    PointElementRecord,
    PointRecord,
    ResourceRecord,
    RoomRecord,
    TourRecord,
    make_session_factory,
    utc_now,
)
from .point_assignments import PointAssignments, apply_assignments, validate_assignments
from .resource_additions import ResourceAdditions


def reverse_delivery(manifest: Path, database_url=None, dry_run=False, assignments: Path | None = None):
    text = manifest.read_text(encoding="utf-8")
    if assignments:
        package = ContentPackage.model_validate_json(text)
        plan = PointAssignments.model_validate_json(assignments.read_text(encoding="utf-8"))
        reverse = plan.model_copy(deep=True)
        for change in reverse.changes:
            change.expected, change.replacement = change.replacement, change.expected
        elements = [item.record.slug for item in package.elements]
        resources = [(item.record.slug, resource.id, package.files[resource.id].sha256) for item in package.elements for resource in item.record.resources]
        links = []
        reference = package.decision.reference
    else:
        additions = ResourceAdditions.model_validate_json(text)
        resources = [(item.element_slug, item.resource.id, item.file.sha256) for item in additions.additions]
        elements, links = [], additions.subtitle_links
        reference = additions.decision.reference
    now = utc_now()
    factory = make_session_factory(database_url)
    grants = CONTENT["authorizations"]
    try:
        with factory() as db:
            if assignments:
                validate_assignments(db, reverse)
            for slug, identifier, sha in resources:
                stored = db.get(ResourceRecord, identifier)
                if stored is None or stored.element_id != slug or stored.sha256 != sha:
                    raise ValueError("Resource changed since the reviewed delivery")
            for field, identifiers in [("resource_id", [item[1] for item in resources]), ("element_id", elements)]:
                column = grants.c[field]
                for identifier in identifiers:
                    if db.scalar(sa.select(grants.c.id).where(column == identifier, grants.c.decision_reference == reference)) is None:
                        raise ValueError("The manifest does not identify the original delivery authorization")
                current = db.execute(sa.select(grants).where(column.in_(identifiers), grants.c.revoked_at.is_(None))).mappings().all()
                if any(row["decision_reference"] != reference for row in current):
                    raise ValueError("A later authorization requires a separate reviewed reversal")
                db.execute(grants.update().where(column.in_(identifiers), grants.c.revoked_at.is_(None))
                    .values(revoked_at=now, revocation_reason="reversed_additive_delivery"))
            for link in links:
                narration = db.get(ResourceRecord, link.narration_id)
                if narration is None or narration.sha256 != link.expected_sha256 or narration.subtitles_resource_id != link.subtitles_id:
                    raise ValueError("Narration link changed after delivery")
                narration.subtitles_resource_id = link.expected_subtitles_id
            if assignments:
                apply_assignments(db, reverse, now)
            keys = set(db.scalars(sa.select(RoomRecord.tour_id).join(PointRecord, PointRecord.room_id == RoomRecord.id)
                .join(PointElementRecord, PointElementRecord.point_id == PointRecord.id)
                .where(PointElementRecord.element_id.in_([item[0] for item in resources]))))
            for key in keys:
                tour = db.get(TourRecord, key)
                if tour is not None:
                    tour.revision = now
            if elements:
                db.execute(CONTENT["index_fragments"].delete().where(CONTENT["index_fragments"].c.element_id.in_(elements)))
            # Keep append-only identity records and immutable objects for audit. The public
            # authorization views immediately stop new and already-signed deliveries.
            db.flush()
            if dry_run:
                db.rollback()
            else:
                db.commit()
    finally:
        factory.kw["bind"].dispose()
    return {"dry_run": dry_run, "resources_revoked": len(resources), "elements_revoked": len(elements), "historical_grants_reactivated": 0}
