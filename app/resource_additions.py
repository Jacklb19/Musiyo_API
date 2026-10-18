"""Explicit additive resource transactions; existing publication grants are immutable."""
import json
import shutil
from pathlib import Path
from typing import Literal
from uuid import uuid4

import sqlalchemy as sa
from pydantic import Field, field_validator

from .content_operations import checked_files, digest, grant
from .content_package import ContentPackage, ImportFile, PublicationDecision
from .contracts import ContractModel, Identifier, Resource
from .db import (
    PointElementRecord,
    PointRecord,
    ResourceRecord,
    RoomRecord,
    TourRecord,
    make_session_factory,
    storage_root,
    utc_now,
)
from .model_audit import audit_glb
from .public_repository import PublicRepository
from .resource_storage import configured_s3
from .timed_text import parse_vtt


class ResourceAddition(ContractModel):
    element_slug: Identifier
    resource: Resource
    file: ImportFile
    source_model_id: Identifier | None = None
    expected_parent_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SubtitleLink(ContractModel):
    narration_id: Identifier
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    subtitles_id: Identifier
    expected_subtitles_id: Identifier | None = None


class ResourceAdditions(ContractModel):
    operation_version: Literal[1]
    decision: PublicationDecision
    additions: list[ResourceAddition] = Field(min_length=1)
    subtitle_links: list[SubtitleLink] = Field(default_factory=list)

    @field_validator("operation_version", mode="before")
    @classmethod
    def exact_version_type(cls, value):
        if type(value) is not int:
            raise ValueError("Operation version must be an integer")
        return value


def append_resources(manifest: Path, database_url=None, dry_run=False, private_storage=None):
    manifest = manifest.resolve()
    package = ResourceAdditions.model_validate_json(manifest.read_text(encoding="utf-8"))
    # Reuse the established content/file validation without pretending existing parents
    # belong to this package or requiring their reimport and reauthorization.
    checked = {}
    ids = [item.resource.id for item in package.additions]
    if len(set(ids)) != len(ids) or len({link.narration_id for link in package.subtitle_links}) != len(package.subtitle_links):
        raise ValueError("Repeated addition or subtitle link")
    for item in package.additions:
        resource = item.resource
        if resource.kind not in {"model_3d", "subtitles", "image"} or resource.subtitles_resource_id or resource.variants:
            raise ValueError("Only new model variants, traced thumbnails and subtitle resources are supported")
        if resource.kind == "model_3d" and (not item.file.variant_of or item.file.profile not in {"web", "quest"}):
            raise ValueError("A model addition requires an existing parent and explicit profile")
        if resource.kind == "subtitles" and (item.file.variant_of or item.file.profile != "general"):
            raise ValueError("Subtitles require the general profile")
        if resource.kind == "image" and (not item.source_model_id or not item.expected_parent_sha256 or item.file.variant_of or item.file.profile != "general"):
            raise ValueError("A thumbnail requires a source model, expected hash and general profile")
        if resource.kind != "image" and item.source_model_id:
            raise ValueError("Source model references belong to thumbnails only")
        validation_file = item.file.model_dump()
        validation_file["variant_of"] = None
        value = {"manifest_version": 1, "decision": package.decision.model_dump(mode="json"),
            "elements": [{"kind": "other", "record": {"slug": item.element_slug,
                "title": "Resource append", "description": "Resource validation",
                "resources": [resource.model_dump(mode="json")]}}], "files": {resource.id: validation_file}}
        validated = ContentPackage.model_validate_json(json.dumps(value))
        checked.update(checked_files(validated, manifest.parent))
    now = utc_now()
    if configured_s3() is not None:
        raise ValueError("Additive operations currently require explicitly selected local private storage")
    if package.decision.decision_date > now.date() or not package.decision.approved_culturally or package.decision.authorized_from > now or (package.decision.authorized_until is not None and package.decision.authorized_until <= now):
        raise ValueError("An effective approved decision is required")
    factory = make_session_factory(database_url)
    url = factory.kw["bind"].url
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:" and not Path(url.database).is_file():
        raise ValueError("Select an existing migrated database before adding resources")
    storage = (private_storage or storage_root()).resolve()
    revisions = {}
    try:
        with factory() as db:
            repository = PublicRepository(db)
            for item in package.additions:
                resource, definition = item.resource, item.file
                current = repository.element(item.element_slug)
                if current is None:
                    raise ValueError("The parent element must currently be published")
                if db.get(ResourceRecord, resource.id) is not None:
                    raise ValueError("Existing resource IDs cannot be replaced or reactivated")
                if definition.room_key and db.get(RoomRecord, definition.room_key) is None or definition.point_key and db.get(PointRecord, definition.point_key) is None:
                    raise ValueError("Unknown resource location")
                if resource.kind == "model_3d":
                    parent = db.get(ResourceRecord, definition.variant_of)
                    available = {row["id"] for row in repository.resources(item.element_slug)}
                    if parent is None or parent.id not in available or parent.kind != "model_3d" or parent.variant_of or parent.sha256 != item.expected_parent_sha256:
                        raise ValueError("Published model parent or expected hash differs")
                    if parent.element_id != item.element_slug or parent.sha256 == definition.sha256:
                        raise ValueError("A variant must be a distinct derivative of the same element")
                    if parent.profile == definition.profile or db.scalar(sa.select(ResourceRecord.id).where(
                        ResourceRecord.variant_of == parent.id, ResourceRecord.profile == definition.profile)):
                        raise ValueError("A variant profile already exists, including withdrawn variants")
                if resource.kind == "image":
                    parent = db.get(ResourceRecord, item.source_model_id)
                    available = {variant["id"] for root in current.resources for variant in
                        ([{"id": root.id}] + [variant.model_dump() for variant in root.variants])}
                    if parent is None or parent.id not in available or parent.kind != "model_3d" or parent.element_id != item.element_slug or parent.sha256 != item.expected_parent_sha256:
                        raise ValueError("Thumbnail source model must still be published with the expected hash")
                path, size, object_key = checked[resource.id]
                if resource.kind == "model_3d":
                    audit_glb(path, definition.profile)
                if resource.kind == "subtitles":
                    parse_vtt(path.read_text(encoding="utf-8-sig"))
                values = resource.model_dump(exclude={"id", "variants"})
                stored = ResourceRecord(id=resource.id, element_id=item.element_slug, name=path.name,
                    private_path=object_key, **values)
                stored.sha256, stored.byte_count = definition.sha256, size
                stored.profile, stored.variant_of = definition.profile, definition.variant_of
                stored.room_id, stored.point_id = definition.room_key, definition.point_key
                stored.paradata, stored.technical_metadata = definition.paradata, definition.metadata
                db.add(stored)
                db.flush()
                grant(db, package.decision, now, resource_id=resource.id, allow_assistant=False)
            for link in package.subtitle_links:
                narration = db.get(ResourceRecord, link.narration_id)
                subtitle = db.get(ResourceRecord, link.subtitles_id)
                if narration is None or narration.kind != "narration" or narration.sha256 != link.expected_sha256 or narration.subtitles_resource_id != link.expected_subtitles_id:
                    raise ValueError("Narration or expected subtitle link differs")
                available = {row["id"] for row in repository.resources(narration.element_id)}
                if narration.id not in available or subtitle is None or subtitle.id not in ids or subtitle.element_id != narration.element_id or subtitle.kind != "subtitles":
                    raise ValueError("The new subtitles and published narration must belong to the same element")
                cues = parse_vtt(checked[subtitle.id][0].read_text(encoding="utf-8-sig"))
                if narration.duration_seconds is None or cues[-1][1] > narration.duration_seconds + .05:
                    raise ValueError("Subtitles exceed the measured narration duration")
                if " ".join(cue[2] for cue in cues).split() != (narration.transcription or "").split():
                    raise ValueError("Subtitles must preserve the exact transcript words and punctuation")
                narration.subtitles_resource_id = subtitle.id
            keys = set(db.scalars(sa.select(RoomRecord.tour_id).join(PointRecord, PointRecord.room_id == RoomRecord.id)
                .join(PointElementRecord, PointElementRecord.point_id == PointRecord.id)
                .where(PointElementRecord.element_id.in_([item.element_slug for item in package.additions]))))
            for key in keys:
                tour = db.get(TourRecord, key)
                if tour is not None:
                    tour.revision = now
                    revisions[key] = now.isoformat()
            db.flush()
            if dry_run:
                db.rollback()
            else:
                for path, _, object_key in checked.values():
                    target = (storage / object_key).resolve()
                    target.relative_to(storage)
                    if target.exists():
                        if digest(target) != Path(object_key).stem:
                            raise ValueError("Private storage checksum differs")
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + "." + str(uuid4()) + ".tmp")
                    try:
                        shutil.copyfile(path, temporary)
                        if digest(temporary) != Path(object_key).stem:
                            raise ValueError("Resource changed during append")
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                db.commit()
    finally:
        factory.kw["bind"].dispose()
    return {"dry_run": dry_run, "resources": len(ids), "subtitle_links": len(package.subtitle_links), "tour_revisions": revisions}
