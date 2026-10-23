"""Public content is read through publication views, including direct identifiers."""
from datetime import timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from .catalog_search import local_rank
from .content_tables import CONTENT
from .contracts import (
    CatalogFacet,
    CatalogItem,
    CatalogPage,
    Element,
    ElementSummary,
    Guide,
    NamedTerm,
    Room,
    Tour,
    TourMetadata,
)
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
            connection.create_function("musiyo_catalog_rank", 4, local_rank, deterministic=True)

    def ready(self) -> bool:
        revision = self.db.scalar(sa.text("SELECT version_num FROM alembic_version"))
        self.db.execute(sa.text("SELECT id FROM v_assistant_elements LIMIT 0"))
        # 0006 only adds the anonymous survey table; the public read model is the same.
        return revision in ("0005", "0006")

    def facets(self, name: str) -> list[CatalogFacet]:
        term = CONTENT[name]
        metadata = CONTENT["element_metadata"] if name == "categories" else CONTENT["collection_elements"]
        foreign_key = metadata.c.category_id if name == "categories" else metadata.c.collection_id
        rows = self.db.execute(sa.select(term.c.slug, term.c.name,
            sa.func.count(sa.distinct(ELEMENTS.c.id)).label("element_count"))
            .join(metadata, foreign_key == term.c.id).join(ELEMENTS, ELEMENTS.c.id == metadata.c.element_id)
            .group_by(term.c.id, term.c.slug, term.c.name, term.c.order).order_by(term.c.order, term.c.slug)).mappings()
        return [CatalogFacet.model_validate(dict(row)) for row in rows]

    def catalog(self, query: str = "", category: str | None = None, collection: str | None = None,
                highlighted: bool = False, limit: int = 24, offset: int = 0) -> CatalogPage:
        search, metadata, details = CONTENT["element_search"], CONTENT["element_metadata"], CONTENT["element_details"]
        categories, communities = CONTENT["categories"], CONTENT["communities"]
        statement = sa.select(ELEMENTS.c.id.label("slug"), sa.func.coalesce(details.c.title, ELEMENTS.c.title).label("title"),
            sa.func.coalesce(details.c.description, ELEMENTS.c.description).label("description"),
            categories.c.slug.label("category_slug"), categories.c.name.label("category_name"),
            communities.c.name.label("community")).select_from(ELEMENTS)
        statement = statement.outerjoin(details, details.c.element_id == ELEMENTS.c.id)
        statement = statement.outerjoin(metadata, metadata.c.element_id == ELEMENTS.c.id)
        statement = statement.outerjoin(categories, categories.c.id == metadata.c.category_id)
        statement = statement.outerjoin(communities, communities.c.id == metadata.c.community_id)
        if category:
            statement = statement.where(categories.c.slug == category)
        if collection:
            links, terms = CONTENT["collection_elements"], CONTENT["collections"]
            statement = statement.where(sa.exists(sa.select(1).select_from(links)
                .join(terms, terms.c.id == links.c.collection_id)
                .where(links.c.element_id == ELEMENTS.c.id, terms.c.slug == collection)))
        if highlighted:
            statement = statement.where(metadata.c.highlighted.is_(True))
        rank = None
        query = query.strip()
        if query:
            statement = statement.join(search, search.c.element_id == ELEMENTS.c.id)
            if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
                self.db.execute(sa.select(sa.func.set_config("pg_trgm.similarity_threshold", "0.3", True)))
                terms_query = sa.func.websearch_to_tsquery(sa.literal_column("'public.spanish_unaccent'"), query)
                normalized = sa.func.public.unaccent(sa.func.lower(query))
                match = search.c.search_vector.bool_op("@@")(terms_query)
                fuzzy = search.c.normalized_title.bool_op("%")(normalized)
                rank = sa.func.ts_rank_cd(search.c.search_vector, terms_query) * 10 + sa.func.similarity(search.c.normalized_title, normalized)
                statement = statement.where(sa.or_(match, fuzzy))
            else:
                rank = sa.func.musiyo_catalog_rank(search.c.title, search.c.terms, search.c.body, query)
                statement = statement.where(rank > 0)
        total = self.db.scalar(sa.select(sa.func.count()).select_from(statement.subquery())) or 0
        statement = statement.order_by(rank.desc(), ELEMENTS.c.id) if rank is not None else statement.order_by(ELEMENTS.c.id)
        rows = self.db.execute(statement.limit(limit).offset(offset)).mappings().all()
        slugs = [row["slug"] for row in rows]
        resources = self.db.execute(sa.select(RESOURCES.c.element_id, RESOURCES.c.id, RESOURCES.c.kind)
            .where(RESOURCES.c.element_id.in_(slugs), RESOURCES.c.variant_of.is_(None)).order_by(RESOURCES.c.id)).mappings().all() if slugs else []
        items = []
        for row in rows:
            available = [item for item in resources if item["element_id"] == row["slug"]]
            items.append(CatalogItem(slug=row["slug"], title=row["title"], description=row["description"], community=row["community"],
                category=NamedTerm(slug=row["category_slug"], name=row["category_name"]) if row["category_slug"] else None,
                thumbnail_resource_id=next((item["id"] for item in available if item["kind"] == "image"), None),
                has_3d_model=any(item["kind"] == "model_3d" for item in available),
                has_narration=any(item["kind"] == "narration" for item in available)))
        return CatalogPage(schema_version=1, items=items, total=total, limit=limit, offset=offset)

    def resource(self, element_id: str, resource_id: str):
        return self.db.execute(sa.select(RESOURCES).where(
            RESOURCES.c.id == resource_id, RESOURCES.c.element_id == element_id)).mappings().first()

    def resource_by_id(self, resource_id: str):
        return self.db.execute(sa.select(RESOURCES).where(RESOURCES.c.id == resource_id)).mappings().first()

    def resources(self, element_id: str):
        return self.db.execute(sa.select(RESOURCES).where(RESOURCES.c.element_id == element_id)
            .order_by(RESOURCES.c.id)).mappings().all()

    def element(self, slug: str) -> Element | None:
        record = self.db.execute(sa.select(ELEMENTS).where(ELEMENTS.c.id == slug)).mappings().first()
        return self.format_elements([record])[0] if record else None

    def elements(self) -> list[Element]:
        rows = self.db.execute(sa.select(ELEMENTS).order_by(ELEMENTS.c.id)).mappings().all()
        return self.format_elements(rows)

    def grouped(self, keys: list[str], statement, column) -> dict[str, list]:
        # One query for every requested parent: an online database pays a round trip per query,
        # and a tour of twenty fichas read one by one cost some two hundred of them (15 s on Render).
        groups: dict[str, list] = {key: [] for key in keys}
        if keys:
            for row in self.db.execute(statement.where(column.in_(keys))).mappings().all():
                groups[row[column.key]].append(row)
        return groups

    def metadata_by_element(self, name: str, keys: list[str]) -> dict[str, list]:
        table = CONTENT[name]
        return self.grouped(keys, sa.select(table).join(ELEMENTS, ELEMENTS.c.id == table.c.element_id), table.c.element_id)

    def format_elements(self, records) -> list[Element]:
        keys = [record["id"] for record in records]
        details = self.metadata_by_element("element_details", keys)
        attributes = self.metadata_by_element("element_metadata", keys)
        blocks = self.metadata_by_element("detail_blocks", keys)
        credits = self.metadata_by_element("credits", keys)
        restrictions = self.metadata_by_element("restrictions", keys)
        sources_table, links = CONTENT["sources"], CONTENT["element_sources"]
        sources = self.grouped(keys, sa.select(sources_table, links.c.element_id)
            .join(links, sources_table.c.id == links.c.source_id)
            .join(ELEMENTS, ELEMENTS.c.id == links.c.element_id).order_by(sources_table.c.id), links.c.element_id)
        collections_table, members = CONTENT["collections"], CONTENT["collection_elements"]
        collections = self.grouped(keys, sa.select(collections_table, members.c.element_id)
            .join(members, collections_table.c.id == members.c.collection_id)
            .join(ELEMENTS, ELEMENTS.c.id == members.c.element_id)
            .order_by(collections_table.c.order, collections_table.c.slug), members.c.element_id)
        resources = self.grouped(keys, sa.select(RESOURCES).order_by(RESOURCES.c.id), RESOURCES.c.element_id)
        terms: dict[str, dict] = {}
        for name, field in (("categories", "category_id"), ("communities", "community_id")):
            ids = {rows[0][field] for rows in attributes.values() if rows and rows[0][field]}
            terms[name] = {row["id"]: row for row in self.db.execute(sa.select(CONTENT[name])
                .where(CONTENT[name].c.id.in_(ids))).mappings().all()} if ids else {}
        authors = {rows[0]["corrected_by"] for rows in details.values() if rows and rows[0]["corrected_by"]}
        validators = CONTENT["validators"]
        names: dict[str, str] = {row["id"]: row["name"] for row in self.db.execute(sa.select(validators.c.id, validators.c.name)
            .where(validators.c.id.in_(authors))).mappings().all()} if authors else {}
        return [self.format_element(record, details[key], attributes[key], sources[key], blocks[key], credits[key],
            restrictions[key], collections[key], resources[key], terms, names)
            for record, key in zip(records, keys)]

    def format_element(self, record, details, attributes, source_rows, blocks, credits, restrictions,
                       collections, available, terms, names) -> Element:
        key = record["id"]
        detail = details[0] if details else record
        attributes = attributes[0] if attributes else {}
        sources: list[dict[str, Any]] = [{field: row[field] for field in ("id", "kind", "reference", "url")} for row in source_rows]
        blocks = sorted(blocks, key=lambda block: block["order"])
        credits = sorted(credits, key=lambda credit: (credit["order"], credit["id"]))
        category = terms["categories"].get(attributes.get("category_id"))
        community = terms["communities"].get(attributes.get("community_id"))
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
            author = names.get(detail["corrected_by"])
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
        room_ids = [room["id"] for room in rooms]
        points = self.grouped(room_ids, sa.select(POINTS).order_by(POINTS.c.order, POINTS.c.id), POINTS.c.room_id)
        point_ids = [point["id"] for room_points in points.values() for point in room_points]
        links = self.grouped(point_ids, sa.select(LINKS).order_by(LINKS.c.order, LINKS.c.element_id), LINKS.c.point_id)
        ambient = self.grouped(room_ids, sa.select(RESOURCES.c.id, RESOURCES.c.room_id)
            .where(RESOURCES.c.kind == "ambient_audio").order_by(RESOURCES.c.id), RESOURCES.c.room_id)
        result = []
        for room in rooms:
            point_contracts = [{"key": point["id"], "name": point["name"] or point["id"],
                "order": point["order"], "activation": point["activation"],
                "elements": [summaries[link["element_id"]] for link in links[point["id"]] if link["element_id"] in summaries]}
                for point in points[room["id"]]]
            result.append(Room.model_validate({"key": room["id"], "name": room["name"] or room["id"],
                "order": room["order"], "short_description": room["short_description"],
                "ambient_audio_resource_id": ambient[room["id"]][0]["id"] if ambient[room["id"]] else None,
                "points": point_contracts}))
        guide_key, guide_name, guide_room = tour["guide_key"], tour["guide_name"], tour["guide_room_key"]
        guide = Guide(key=guide_key, name=guide_name, room_key=guide_room, available=tour["guide_available"]) \
            if guide_key and guide_name and guide_room and guide_room in {room.key for room in result} else None
        return Tour(schema_version=1, tour=TourMetadata(key=key, name=tour["name"], revision=tour["revision"]), rooms=result, guide=guide)
