"""Public content is read through publication views, including direct identifiers."""
from datetime import timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from .content_tables import CONTENT
from .contracts import Element, ElementSummary, Guide, Room, Tour, TourMetadata
from .db import utc_now


def view(name: str, entity: str):
    return sa.table(name, *(sa.column(column.name, column.type) for column in CONTENT[entity].columns))


ELEMENTS = view("v_current_elements", "elements")
RESOURCES = view("v_current_resources", "resources")
TOURS = view("v_public_tours", "tours")
ROOMS = view("v_public_rooms", "rooms")
POINTS = view("v_public_points", "points")
LINKS = view("v_public_point_elements", "point_elements")
ASSISTANT_ELEMENTS = view("v_assistant_elements", "elements")


class PublicRepository:
    def __init__(self, session: Session, now=utc_now):
        self.db = session
        if session.bind is not None and session.bind.dialect.name == "sqlite":
            instant = now().astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
            connection = session.connection().connection.driver_connection
            if connection is None:
                raise RuntimeError("SQLite connection is unavailable")
            connection.create_function("musiyo_now", 0, lambda: instant)

    def metadata(self, name: str, element_id: str):
        table = CONTENT[name]
        return self.db.execute(sa.select(table).join(ELEMENTS, ELEMENTS.c.id == table.c.element_id)
            .where(ELEMENTS.c.id == element_id)).mappings().all()

    def resource(self, element_id: str, resource_id: str):
        return self.db.execute(sa.select(RESOURCES).where(
            RESOURCES.c.id == resource_id, RESOURCES.c.element_id == element_id)).mappings().first()

    def resources(self, element_id: str):
        return self.db.execute(sa.select(RESOURCES).where(RESOURCES.c.element_id == element_id)
            .order_by(RESOURCES.c.id)).mappings().all()

    def element(self, slug: str) -> Element | None:
        record = self.db.execute(sa.select(ELEMENTS).where(ELEMENTS.c.id == slug)).mappings().first()
        return self.format_element(record) if record else None

    def elements(self) -> list[Element]:
        rows = self.db.execute(sa.select(ELEMENTS).order_by(ELEMENTS.c.id)).mappings().all()
        return [self.format_element(row) for row in rows]

    def format_element(self, record) -> Element:
        key = record["id"]
        details = self.metadata("element_details", key)
        detail = details[0] if details else record
        attributes = self.metadata("element_metadata", key)
        attributes = attributes[0] if attributes else {}
        sources_table, links = CONTENT["sources"], CONTENT["element_sources"]
        source_rows = self.db.execute(sa.select(sources_table).join(links, sources_table.c.id == links.c.source_id)
            .join(ELEMENTS, ELEMENTS.c.id == links.c.element_id).where(ELEMENTS.c.id == key)
            .order_by(sources_table.c.id)).mappings().all()
        sources: list[dict[str, Any]] = [{field: row[field] for field in ("id", "kind", "reference", "url")} for row in source_rows]
        blocks = sorted(self.metadata("detail_blocks", key), key=lambda block: block["order"])
        credits = sorted(self.metadata("credits", key), key=lambda credit: (credit["order"], credit["id"]))
        restrictions = self.metadata("restrictions", key)
        category = self.db.execute(sa.select(CONTENT["categories"]).where(
            CONTENT["categories"].c.id == attributes.get("category_id"))).mappings().first()
        community = self.db.execute(sa.select(CONTENT["communities"]).where(
            CONTENT["communities"].c.id == attributes.get("community_id"))).mappings().first()
        collections = self.db.execute(sa.select(CONTENT["collections"])
            .join(CONTENT["collection_elements"], CONTENT["collections"].c.id == CONTENT["collection_elements"].c.collection_id)
            .join(ELEMENTS, ELEMENTS.c.id == CONTENT["collection_elements"].c.element_id)
            .where(ELEMENTS.c.id == key).order_by(CONTENT["collections"].c.order, CONTENT["collections"].c.slug)).mappings().all()
        available = self.resources(key)
        available_ids = {item["id"] for item in available}
        credits = [item for item in credits if not item["resource_id"] or item["resource_id"] in available_ids]
        restrictions = [item for item in restrictions if not item["resource_id"] or item["resource_id"] in available_ids]
        resources = []
        for resource in available:
            if resource["kind"] is None or resource["kind"] == "ambient_audio" or resource["variant_of"] is not None:
                continue
            variants = [{"id": item["id"], "profile": item["profile"]} for item in available
                if (item["variant_of"] == resource["id"] or item["id"] == resource["id"])
                and item["profile"] in ("web", "quest")]
            resources.append({"id": resource["id"], "kind": resource["kind"], "mime": resource["mime"],
                "byte_count": resource["byte_count"], "duration_seconds": resource["duration_seconds"],
                "alternative_text": resource["alternative_text"], "credit": resource["credit"],
                "provenance": resource["provenance"], "transcription": resource["transcription"], "variants": variants,
                "subtitles_resource_id": resource["subtitles_resource_id"] if any(
                    item["id"] == resource["subtitles_resource_id"] for item in available) else None})
        modified = None
        if details and detail["corrected_by"] and detail["corrected_at"]:
            author = self.db.scalar(sa.select(CONTENT["validators"].c.name).where(CONTENT["validators"].c.id == detail["corrected_by"]))
            if author:
                modified = {"date": detail["corrected_at"], "author": author}
        # Rows created by the prototype after migration keep their existing text only.
        if not details:
            sources = [{"id": "legacy-source", "kind": "other", "reference": record["sources"], "url": None}] if record["sources"] else []
            blocks = [{"id": "legacy-interpretation", "kind": "interpretation", "text": record["interpretation"],
                "source_id": "legacy-source" if sources else None, "attribution": None, "context": None}] if record["interpretation"] else []
            credits = [{"role": None, "name": record["credits"]}] if record["credits"] else []
            restrictions = [{"kind": "other", "description": record["restrictions"]}] if record["restrictions"] else []
        return Element.model_validate({"slug": key, "title": detail["title"], "description": detail["description"],
            "community": {"name": community["name"], "people": community["people"]} if community else None,
            "category": {"slug": category["slug"], "name": category["name"]} if category else None,
            "collections": [{"slug": item["slug"], "name": item["name"]} for item in collections],
            "technique": attributes.get("technique"), "materials": attributes.get("materials", []),
            "blocks": [{field: block[field] for field in ("id", "kind", "text", "source_id", "attribution", "context")} for block in blocks],
            "sources": [{"id": source["id"], "kind": source["kind"] if source["kind"] in ("publication", "interview") else "other",
                "reference": source["reference"], "url": source["url"]} for source in sources],
            "credits": [{"role": credit["role"], "name": credit["name"]} for credit in credits],
            "restrictions": [{"kind": restriction["kind"] if restriction["kind"] in ("no_download", "no_reuse") else "other",
                "description": restriction["description"]} for restriction in restrictions],
            "resources": resources, "last_modified": modified})

    def tour(self, key: str) -> Tour | None:
        tour = self.db.execute(sa.select(TOURS).where(TOURS.c.id == key)).mappings().first()
        if tour is None:
            return None
        rooms = self.db.execute(sa.select(ROOMS).where(ROOMS.c.tour_id == key)
            .order_by(ROOMS.c.order, ROOMS.c.id)).mappings().all()
        if not rooms:
            return None
        elements = {element.slug: element for element in self.elements()}
        summaries = {slug: ElementSummary(slug=slug, title=element.title,
            community=element.community.name if element.community else None,
            thumbnail_resource_id=next((item.id for item in element.resources if item.kind == "image"), None),
            has_3d_model=any(item.kind == "model_3d" for item in element.resources),
            has_narration=any(item.kind == "narration" for item in element.resources)) for slug, element in elements.items()}
        result = []
        for room in rooms:
            points = self.db.execute(sa.select(POINTS).where(POINTS.c.room_id == room["id"])
                .order_by(POINTS.c.order, POINTS.c.id)).mappings().all()
            point_contracts = []
            for point in points:
                links = self.db.execute(sa.select(LINKS).where(LINKS.c.point_id == point["id"])
                    .order_by(LINKS.c.order, LINKS.c.element_id)).mappings().all()
                point_contracts.append({"key": point["id"], "name": point["name"] or point["id"],
                    "order": point["order"], "activation": point["activation"],
                    "elements": [summaries[link["element_id"]] for link in links if link["element_id"] in summaries]})
            ambient = self.db.scalar(sa.select(RESOURCES.c.id).where(RESOURCES.c.room_id == room["id"],
                RESOURCES.c.kind == "ambient_audio").order_by(RESOURCES.c.id))
            result.append(Room.model_validate({"key": room["id"], "name": room["name"] or room["id"],
                "order": room["order"], "short_description": room["short_description"],
                "ambient_audio_resource_id": ambient, "points": point_contracts}))
        guide_key, guide_name, guide_room = tour["guide_key"], tour["guide_name"], tour["guide_room_key"]
        guide = Guide(key=guide_key, name=guide_name, room_key=guide_room, available=tour["guide_available"]) \
            if guide_key and guide_name and guide_room and guide_room in {room.key for room in result} else None
        return Tour(schema_version=1, tour=TourMetadata(key=key, name=tour["name"], revision=tour["revision"]), rooms=result, guide=guide)
