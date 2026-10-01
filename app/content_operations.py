"""Local content administration; public responses remain behind validity views."""
import hashlib
import shutil
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from argon2 import PasswordHasher

from .content_package import ContentPackage
from .content_tables import CONTENT
from .db import (
    ElementRecord,
    PointElementRecord,
    PointRecord,
    ResourceRecord,
    RoomRecord,
    TourRecord,
    make_session_factory,
    storage_root,
    utc_now,
)
from .public_repository import ASSISTANT_ELEMENTS, PublicRepository
from .resource_storage import configured_s3


def upsert(db, table, identity, values):
    condition = sa.and_(*(table.c[key] == value for key, value in identity.items()))
    if db.execute(sa.select(table).where(condition)).first():
        db.execute(table.update().where(condition).values(**values))
    else:
        db.execute(table.insert().values(**identity, **values))


def term(db, name, value):
    table = CONTENT[name]
    row = db.execute(sa.select(table).where(table.c.slug == value.slug)).mappings().first()
    if row:
        if row["name"] != value.name:
            raise ValueError("Existing term differs from the package")
        return row["id"]
    identifier = str(uuid4())
    db.execute(table.insert().values(id=identifier, slug=value.slug, name=value.name))
    return identifier


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_files(package, folder):
    checked = {}
    resources = {resource.id: resource for item in package.elements for resource in item.record.resources}
    for identifier, definition in package.files.items():
        path = (folder / definition.file).resolve()
        try:
            path.relative_to(folder)
        except ValueError:
            raise ValueError("Resource file is outside the package")
        resource = resources[identifier]
        if not path.is_file() or digest(path) != definition.sha256:
            raise ValueError("Resource is missing or its checksum differs")
        size = path.stat().st_size
        limit = {"model_3d": 6_000_000 if definition.profile == "quest" else 10_000_000,
            "video": 40_000_000, "image": 800_000}.get(resource.kind, 3_000_000)
        if not size or size > limit or resource.byte_count is not None and resource.byte_count != size:
            raise ValueError("Resource size differs or exceeds its platform budget")
        mime_extensions = {"model/gltf-binary": ".glb", "image/jpeg": ".jpg", "image/png": ".png",
            "image/webp": ".webp", "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/ogg": ".ogg",
            "video/mp4": ".mp4", "text/vtt": ".vtt"}
        if resource.mime not in mime_extensions:
            raise ValueError("Unsupported resource MIME type")
        expected = {"model_3d": "model/", "image": "image/", "audio": "audio/", "narration": "audio/",
            "video": "video/", "subtitles": "text/vtt"}[resource.kind]
        if not resource.mime.startswith(expected):
            raise ValueError("Resource kind and MIME type differ")
        with path.open("rb") as stream:
            header = stream.read(32)
        valid = {"model/gltf-binary": header.startswith(b"glTF") and header[4:8] == b"\x02\x00\x00\x00" and int.from_bytes(header[8:12], "little") == size,
            "image/jpeg": header.startswith(b"\xff\xd8\xff"), "image/png": header.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
            "audio/mpeg": header.startswith(b"ID3") or len(header) > 1 and header[0] == 255 and header[1] & 224 == 224,
            "audio/wav": header.startswith(b"RIFF") and header[8:12] == b"WAVE", "audio/ogg": header.startswith(b"OggS"),
            "video/mp4": header[4:8] == b"ftyp", "text/vtt": header.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT")}[resource.mime]
        if not valid:
            raise ValueError("Resource header does not match its MIME type")
        checked[identifier] = (path, size, "objects/" + definition.sha256[:2] + "/" + definition.sha256 + mime_extensions[resource.mime])
    return checked


def import_package(manifest: Path, database_url=None, dry_run=False, private_storage=None):
    manifest = manifest.resolve()
    package = ContentPackage.model_validate_json(manifest.read_text(encoding="utf-8"))
    files = checked_files(package, manifest.parent)
    factory = make_session_factory(database_url)
    url = factory.kw["bind"].url
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:" and not Path(url.database).is_file():
        raise ValueError("Run musiyo migrate before importing content")
    storage = (private_storage or storage_root()).resolve()
    object_storage = configured_s3() if not dry_run else None
    now = utc_now()
    if package.decision.decision_date > now.date():
        raise ValueError("Decision date is in the future")
    with factory() as db:
        try:
            for item in package.elements:
                record = item.record
                for key in item.point_keys:
                    if db.get(PointRecord, key) is None:
                        raise ValueError("Unknown point in the package")
                element = db.get(ElementRecord, record.slug)
                if element is None:
                    element = ElementRecord(id=record.slug, title=record.title, description=record.description)
                    db.add(element)
                element.title, element.description = record.title, record.description
                db.flush()
                metadata = {"kind": item.kind, "technique": record.technique, "materials": record.materials,
                    "category_id": term(db, "categories", record.category) if record.category else None, "community_id": None}
                if record.community:
                    table = CONTENT["communities"]
                    community = db.scalar(sa.select(table.c.id).where(table.c.name == record.community.name, table.c.people == record.community.people))
                    if community is None:
                        community = str(uuid4())
                        db.execute(table.insert().values(id=community, name=record.community.name, people=record.community.people))
                    metadata["community_id"] = community
                upsert(db, CONTENT["element_metadata"], {"element_id": record.slug}, metadata)
                upsert(db, CONTENT["element_details"], {"element_id": record.slug}, {"title": record.title, "description": record.description})
                for name in ("index_fragments", "detail_blocks", "element_sources", "collection_elements", "credits", "restrictions"):
                    db.execute(CONTENT[name].delete().where(CONTENT[name].c.element_id == record.slug))
                for source in record.sources:
                    table = CONTENT["sources"]
                    existing = db.execute(sa.select(table).where(table.c.id == source.id)).mappings().first()
                    values = source.model_dump(exclude={"id"})
                    if existing and any(existing[field] != value for field, value in values.items()):
                        raise ValueError("Existing source differs from the package")
                    if not existing:
                        db.execute(table.insert().values(id=source.id, **values))
                    db.execute(CONTENT["element_sources"].insert().values(element_id=record.slug, source_id=source.id))
                for order, block in enumerate(record.blocks):
                    db.execute(CONTENT["detail_blocks"].insert().values(element_id=record.slug, order=order, **block.model_dump()))
                for order, collection in enumerate(record.collections):
                    db.execute(CONTENT["collection_elements"].insert().values(element_id=record.slug, collection_id=term(db, "collections", collection), order=order))
                for order, credit in enumerate(record.credits):
                    db.execute(CONTENT["credits"].insert().values(element_id=record.slug, order=order, **credit.model_dump()))
                restrictions = [restriction.model_dump() for restriction in record.restrictions]
                if not item.show_in_tour:
                    restrictions.append({"kind": "no_tour", "description": "No disponible en el recorrido."})
                for restriction in restrictions:
                    db.execute(CONTENT["restrictions"].insert().values(element_id=record.slug, **restriction))
                previous_links = {link.point_id: link.order for link in db.scalars(sa.select(PointElementRecord).where(PointElementRecord.element_id == record.slug))}
                db.execute(sa.delete(PointElementRecord).where(PointElementRecord.element_id == record.slug))
                for key in item.point_keys:
                    point_order = previous_links.get(key)
                    if point_order is None:
                        maximum = db.scalar(sa.select(sa.func.max(PointElementRecord.order)).where(PointElementRecord.point_id == key))
                        point_order = (maximum if maximum is not None else -1) + 1
                    db.add(PointElementRecord(point_id=key, element_id=record.slug, order=point_order))
                grant(db, package.decision, now, element_id=record.slug, allow_assistant=item.allow_assistant)
                for resource in sorted(record.resources, key=lambda value: (package.files[value.id].variant_of is not None, value.kind != "subtitles")):
                    definition = package.files[resource.id]
                    if definition.room_key and db.get(RoomRecord, definition.room_key) is None or definition.point_key and db.get(PointRecord, definition.point_key) is None:
                        raise ValueError("Unknown resource parent")
                    stored = db.get(ResourceRecord, resource.id)
                    if stored and stored.element_id != record.slug:
                        raise ValueError("Resource belongs to another element")
                    path, size, object_key = files[resource.id]
                    if stored is None:
                        stored = ResourceRecord(id=resource.id, element_id=record.slug, name=path.name, mime=resource.mime, private_path=object_key)
                        db.add(stored)
                    for field in ("kind", "mime", "duration_seconds", "alternative_text", "transcription", "credit", "provenance", "subtitles_resource_id"):
                        setattr(stored, field, getattr(resource, field))
                    stored.private_path, stored.byte_count, stored.sha256 = object_key, size, definition.sha256
                    stored.profile, stored.variant_of = definition.profile, definition.variant_of
                    stored.room_id, stored.point_id = definition.room_key, definition.point_key
                    stored.paradata, stored.technical_metadata = definition.paradata, definition.metadata
                    db.flush()
                    grant(db, package.decision, now, resource_id=resource.id, allow_assistant=item.allow_assistant)
                existing_ids = [row.id for row in db.scalars(sa.select(ResourceRecord).where(ResourceRecord.element_id == record.slug)) if row.id not in {item.id for item in record.resources}]
                if existing_ids:
                    db.execute(CONTENT["authorizations"].update().where(CONTENT["authorizations"].c.resource_id.in_(existing_ids))
                        .values(revoked_at=now, revocation_reason="removed_by_import"))
            db.flush()
            if dry_run:
                db.rollback()
            else:
                for path, size, object_key in files.values():
                    target = (storage / object_key).resolve()
                    target.relative_to(storage)
                    if target.exists():
                        if digest(target) != Path(object_key).stem or digest(path) != Path(object_key).stem:
                            raise ValueError("Private storage checksum differs")
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + "." + str(uuid4()) + ".tmp")
                    try:
                        shutil.copyfile(path, temporary)
                        if digest(temporary) != Path(object_key).stem:
                            raise ValueError("Resource changed during import")
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                if object_storage is not None:
                    resources = {resource.id: resource for item in package.elements for resource in item.record.resources}
                    for resource_id, (_, _, object_key) in files.items():
                        object_storage.upload_local(storage / object_key, object_key, resources[resource_id].mime, package.files[resource_id].sha256)
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            factory.kw["bind"].dispose()
    return {"dry_run": dry_run, "elements": len(package.elements), "resources": len(files)}


def grant(db, decision, now, **owner):
    table = CONTENT["authorizations"]
    parent = "element_id" if "element_id" in owner else "resource_id"
    db.execute(table.update().where(table.c[parent] == owner[parent], table.c.revoked_at.is_(None))
        .values(revoked_at=now, revocation_reason="replaced_by_import"))
    db.execute(table.insert().values(**owner, approved_culturally=decision.approved_culturally,
        approved_by=decision.responsible, decision_date=decision.decision_date, decision_reference=decision.reference,
        authorized_from=decision.authorized_from, authorized_until=decision.authorized_until))


def revoke(identifier, resource=False, database_url=None, reason="withdrawn"):
    factory = make_session_factory(database_url)
    table = CONTENT["authorizations"]
    with factory.begin() as db:
        parent = db.get(ResourceRecord if resource else ElementRecord, identifier)
        if parent is None:
            raise ValueError("Content does not exist")
        column = table.c.resource_id if resource else table.c.element_id
        result = db.execute(table.update().where(column == identifier, table.c.revoked_at.is_(None))
            .values(revoked_at=utc_now(), revocation_reason=reason))
        element_id = parent.element_id if resource else parent.id
        db.execute(CONTENT["index_fragments"].delete().where(CONTENT["index_fragments"].c.element_id == element_id))
        return {"revoked": result.rowcount}


def withdraw_tour(key, database_url=None):
    with make_session_factory(database_url).begin() as db:
        tour = db.get(TourRecord, key)
        if tour is None:
            raise ValueError("Tour does not exist")
        tour.published = False
    return {"withdrawn_tour": key}


def verify_validity(database_url=None):
    with make_session_factory(database_url).begin() as db:
        PublicRepository(db)
        table = CONTENT["index_fragments"]
        result = db.execute(table.delete().where(table.c.element_id.not_in(sa.select(ASSISTANT_ELEMENTS.c.id))))
        return {"removed_fragments": result.rowcount}


def reindex(database_url=None, element_id=None):
    with make_session_factory(database_url).begin() as db:
        public = PublicRepository(db)
        allowed = set(db.scalars(sa.select(ASSISTANT_ELEMENTS.c.id)))
        if element_id is not None:
            allowed &= {element_id}
        table = CONTENT["index_fragments"]
        condition = table.c.element_id == element_id if element_id else sa.true()
        db.execute(table.delete().where(condition))
        count = 0
        for slug in sorted(allowed):
            record = public.element(slug)
            if record is None:
                continue
            for block in record.blocks:
                if block.id == "legacy-interpretation":
                    continue
                db.execute(table.insert().values(element_id=slug, block_id=block.id, text=block.text,
                    text_hash=hashlib.sha256(block.text.encode("utf-8")).hexdigest(), embedding_model="pending", embedding=None))
                count += 1
    return {"indexed_fragments": count, "embedding_status": "pending_provider"}


def create_validator(username, name, password, database_url=None):
    if not username.strip() or not name.strip() or len(password) < 12:
        raise ValueError("Validator requires a username, name, and password of at least 12 characters")
    with make_session_factory(database_url).begin() as db:
        table = CONTENT["validators"]
        if db.scalar(sa.select(table.c.id).where(table.c.username == username)):
            raise ValueError("Validator already exists")
        identifier = str(uuid4())
        db.execute(table.insert().values(id=identifier, username=username, name=name, password_hash=PasswordHasher().hash(password)))
    return {"validator_id": identifier}


def export_state(database_url=None):
    with make_session_factory(database_url)() as db:
        public = PublicRepository(db)
        return {"schema_version": 1, "elements": [item.slug for item in public.elements()],
            "counts": {name: db.scalar(sa.select(sa.func.count()).select_from(table)) for name, table in CONTENT.items()},
            "tours": [{"key": tour.id, "published": tour.published} for tour in db.scalars(sa.select(TourRecord).order_by(TourRecord.id))]}
