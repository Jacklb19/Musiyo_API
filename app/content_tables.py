"""Normalized content metadata; legacy entity keys remain stable during migration."""
import json
import math
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.types import UserDefinedType

from .db import Base


class EmbeddingVector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kwargs):
        return "VECTOR(1024)"

    def bind_processor(self, dialect):
        def encode(value):
            if value is None:
                return None
            if len(value) != 1024 or any(not math.isfinite(float(number)) for number in value):
                raise ValueError("Embedding must contain 1024 finite values")
            return json.dumps([float(number) for number in value])
        return encode

    def result_processor(self, dialect, coltype):
        return lambda value: json.loads(value) if isinstance(value, str) else value


def content_tables(metadata):
    def table(name, *columns, **kwargs):
        return sa.Table(name, metadata, *columns,
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp(), onupdate=sa.func.current_timestamp()),
            **kwargs)

    def identity():
        return sa.Column("id", sa.String(36), primary_key=True, default=lambda: str(uuid4()))

    def reference(name, target, nullable=False, **kwargs):
        return sa.Column(name, sa.String(100), sa.ForeignKey(target), nullable=nullable, **kwargs)

    table("communities", identity(), sa.Column("name", sa.Text(), nullable=False),
        sa.Column("people", sa.String(20), nullable=False), sa.Column("description", sa.Text()),
        sa.CheckConstraint("people IN ('kamentsa', 'inga', 'shared')", name="ck_community_people"))
    for name in ("categories", "collections"):
        table(name, identity(), sa.Column("slug", sa.String(100), nullable=False, unique=True),
            sa.Column("name", sa.Text(), nullable=False), sa.Column("description", sa.Text()),
            sa.Column("order", sa.Integer(), nullable=False, server_default="0"))
    table("validators", identity(), sa.Column("name", sa.Text(), nullable=False),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)))
    table("validator_sessions", identity(), reference("validator_id", "validators.id"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False))
    table("element_metadata", reference("element_id", "elements.id", primary_key=True),
        reference("category_id", "categories.id", nullable=True), reference("community_id", "communities.id", nullable=True),
        sa.Column("kind", sa.String(30), nullable=False, server_default="other"),
        sa.Column("technique", sa.Text()), sa.Column("materials", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("highlighted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("kind IN ('mask','character','clothing','dance','song','story','instrument','space','other')", name="ck_element_kind"))
    table("element_details", reference("element_id", "elements.id", primary_key=True),
        sa.Column("language", sa.String(10), nullable=False, server_default="es"),
        sa.Column("title", sa.Text(), nullable=False), sa.Column("description", sa.Text(), nullable=False),
        reference("corrected_by", "validators.id", nullable=True), sa.Column("corrected_at", sa.DateTime(timezone=True)),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.CheckConstraint("language = 'es'", name="ck_detail_language"))
    sa.Table("element_search", metadata, sa.Column("element_id", sa.String(100), sa.ForeignKey("elements.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False), sa.Column("terms", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False), sa.Column("normalized_title", sa.Text()),
        sa.Column("search_vector", sa.Text().with_variant(TSVECTOR(), "postgresql")))
    table("sources", identity(), sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False), sa.Column("url", sa.Text()), sa.Column("year", sa.Integer()),
        sa.CheckConstraint("kind IN ('publication','interview','document','archive','web','other')", name="ck_source_kind"))
    table("element_sources", reference("element_id", "elements.id", primary_key=True),
        reference("source_id", "sources.id", primary_key=True))
    table("detail_blocks", identity(), reference("element_id", "element_details.element_id"),
        sa.Column("kind", sa.String(30), nullable=False), sa.Column("text", sa.Text(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False), reference("source_id", "sources.id", nullable=True),
        sa.Column("attribution", sa.Text()), sa.Column("context", sa.Text()),
        sa.Column("legacy_import", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("element_id", "order", name="uq_block_order"),
        sa.CheckConstraint("kind IN ('documented_fact','testimony','interpretation')", name="ck_block_kind"),
        sa.CheckConstraint("legacy_import OR kind <> 'interpretation' OR source_id IS NOT NULL OR (context IS NOT NULL AND length(trim(context)) > 0)", name="ck_interpretation_context"))
    table("collection_elements", reference("collection_id", "collections.id", primary_key=True),
        reference("element_id", "elements.id", primary_key=True), sa.Column("order", sa.Integer(), nullable=False))
    for name in ("credits", "restrictions"):
        fields = [identity(), reference("element_id", "elements.id", nullable=True), reference("resource_id", "resources.id", nullable=True)]
        if name == "credits":
            fields += [sa.Column("role", sa.Text()), sa.Column("name", sa.Text(), nullable=False), sa.Column("order", sa.Integer(), nullable=False, server_default="0")]
        else:
            fields += [sa.Column("kind", sa.String(30), nullable=False), sa.Column("description", sa.Text(), nullable=False)]
        table(name, *fields, sa.CheckConstraint("element_id IS NOT NULL OR resource_id IS NOT NULL", name=f"ck_{name}_owner"))
    table("authorizations", identity(), reference("element_id", "elements.id", nullable=True, index=True),
        reference("resource_id", "resources.id", nullable=True, index=True),
        sa.Column("approved_culturally", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approved_by", sa.Text()), sa.Column("decision_date", sa.Date()), sa.Column("decision_reference", sa.Text()),
        sa.Column("authorized_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorized_until", sa.DateTime(timezone=True)), sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revocation_reason", sa.Text()),
        sa.Column("allow_assistant", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint("(element_id IS NULL) <> (resource_id IS NULL)", name="ck_authorization_owner"),
        sa.CheckConstraint("authorized_until IS NULL OR authorized_until > authorized_from", name="ck_authorization_interval"))
    table("guides", identity(), sa.Column("key", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False), reference("room_id", "rooms.id"))
    table("index_fragments", identity(), reference("element_id", "elements.id", index=True),
        reference("block_id", "detail_blocks.id", nullable=True), sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.JSON().with_variant(EmbeddingVector(), "postgresql")),
        sa.Column("search_vector", sa.Text().with_variant(TSVECTOR(), "postgresql")),
        sa.Column("text_hash", sa.String(64), nullable=False), sa.Column("embedding_model", sa.Text(), nullable=False))
    return metadata


CONTENT = content_tables(Base.metadata).tables
