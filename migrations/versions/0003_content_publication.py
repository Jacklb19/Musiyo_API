"""Normalized content metadata; legacy entity keys remain stable during migration."""
import json
import math
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.types import UserDefinedType


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


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

RESOURCE_COLUMNS = [
    ("kind", sa.String(30)), ("profile", sa.String(20)),
    ("room_id", sa.String(100)), ("point_id", sa.String(100)),
    ("byte_count", sa.BigInteger()), ("sha256", sa.String(64)),
    ("alternative_text", sa.Text()), ("transcription", sa.Text()),
    ("duration_seconds", sa.Float()), ("credit", sa.Text()), ("provenance", sa.Text()),
    ("accessibility_adaptation", sa.Text()), ("paradata", sa.JSON()), ("metadata", sa.JSON()),
    ("variant_of", sa.String(100)), ("subtitles_resource_id", sa.String(100))
]


def upgrade():
    connection = op.get_bind()
    with op.batch_alter_table("resources") as batch:
        batch.alter_column("element_id", existing_type=sa.String(100), nullable=True)
        for name, datatype in RESOURCE_COLUMNS:
            batch.add_column(sa.Column(name, datatype, nullable=name != "profile",
                server_default="general" if name == "profile" else None))
        for name, target in (("room_id", "rooms"), ("point_id", "points"),
                             ("variant_of", "resources"), ("subtitles_resource_id", "resources")):
            batch.create_foreign_key("fk_resource_" + name, target, [name], ["id"])
        batch.create_check_constraint("ck_resource_profile", "profile IN ('general','web','quest')")
        batch.create_check_constraint("ck_resource_kind", "kind IS NULL OR kind IN ('image','audio','video','model_3d','narration','subtitles','ambient_audio')")
        batch.create_check_constraint("ck_narration_transcript", "kind <> 'narration' OR (transcription IS NOT NULL AND length(trim(transcription)) > 0)")
        batch.create_check_constraint("ck_visual_alternative", "kind NOT IN ('image','model_3d') OR (alternative_text IS NOT NULL AND length(trim(alternative_text)) > 0)")
        batch.create_check_constraint("ck_resource_bytes", "byte_count IS NULL OR byte_count >= 0")
        batch.create_check_constraint("ck_resource_owner", "element_id IS NOT NULL OR room_id IS NOT NULL OR point_id IS NOT NULL")
        batch.create_check_constraint("ck_resource_hash", "sha256 IS NULL OR length(sha256) = 64")
    metadata = sa.MetaData()
    metadata.reflect(connection, only=["elements", "resources", "rooms", "points"])
    previous = set(metadata.tables)
    content_tables(metadata)
    new_tables = [table for table in metadata.sorted_tables if table.name not in previous]
    metadata.create_all(connection, tables=new_tables)
    elements = metadata.tables["elements"]
    resources = metadata.tables["resources"]
    for element in connection.execute(sa.select(elements)).mappings():
        key = element["id"]
        connection.execute(metadata.tables["element_details"].insert().values(
            element_id=key, title=element["title"], description=element["description"]))
        connection.execute(metadata.tables["element_metadata"].insert().values(element_id=key))
        source_id = None
        if element["sources"]:
            source_id = str(uuid4())
            connection.execute(metadata.tables["sources"].insert().values(id=source_id, kind="other", reference=element["sources"]))
            connection.execute(metadata.tables["element_sources"].insert().values(element_id=key, source_id=source_id))
        if element["interpretation"]:
            connection.execute(metadata.tables["detail_blocks"].insert().values(
                element_id=key, kind="interpretation", text=element["interpretation"], order=0,
                source_id=source_id, legacy_import=True))
        if element["credits"]:
            connection.execute(metadata.tables["credits"].insert().values(element_id=key, name=element["credits"]))
        if element["restrictions"]:
            connection.execute(metadata.tables["restrictions"].insert().values(element_id=key, kind="other", description=element["restrictions"]))
        if element["authorized_from"]:
            connection.execute(metadata.tables["authorizations"].insert().values(element_id=key,
                approved_culturally=element["approval_status"] == "approved" and element["approved_at"] is not None,
                authorized_from=element["authorized_from"], authorized_until=element["authorized_until"], revoked_at=element["revoked_at"]))
    for resource in connection.execute(sa.select(resources)).mappings():
        if resource["authorized_from"]:
            connection.execute(metadata.tables["authorizations"].insert().values(resource_id=resource["id"],
                approved_culturally=resource["approved"], authorized_from=resource["authorized_from"],
                authorized_until=resource["authorized_until"], revoked_at=resource["revoked_at"]))
    clock = "musiyo_now()" if connection.dialect.name == "sqlite" else "CURRENT_TIMESTAMP"
    active = f"a.approved_culturally AND a.authorized_from <= {clock} AND (a.authorized_until IS NULL OR {clock} < a.authorized_until) AND a.revoked_at IS NULL"
    views = {
        "v_current_elements": f"SELECT e.* FROM elements e WHERE EXISTS (SELECT 1 FROM authorizations a WHERE a.element_id = e.id AND {active})",
        "v_public_tours": "SELECT t.* FROM tours t WHERE t.published",
        "v_public_rooms": "SELECT r.* FROM rooms r JOIN v_public_tours t ON t.id = r.tour_id",
        "v_public_points": "SELECT p.* FROM points p JOIN v_public_rooms r ON r.id = p.room_id",
        "v_current_resources": f"""SELECT r.* FROM resources r WHERE EXISTS (SELECT 1 FROM authorizations a WHERE a.resource_id = r.id AND {active})
            AND (r.element_id IS NULL OR EXISTS (SELECT 1 FROM v_current_elements e WHERE e.id = r.element_id))
            AND (r.room_id IS NULL OR EXISTS (SELECT 1 FROM v_public_rooms s WHERE s.id = r.room_id))
            AND (r.point_id IS NULL OR EXISTS (SELECT 1 FROM v_public_points p WHERE p.id = r.point_id))""",
        "v_public_point_elements": """SELECT pe.* FROM point_elements pe JOIN v_public_points p ON p.id = pe.point_id
            JOIN v_current_elements e ON e.id = pe.element_id
            WHERE NOT EXISTS (SELECT 1 FROM restrictions r WHERE r.element_id = e.id AND r.kind = 'no_tour')""",
        "v_assistant_elements": f"""SELECT e.* FROM v_current_elements e WHERE NOT EXISTS
            (SELECT 1 FROM restrictions r WHERE r.element_id = e.id AND r.kind = 'exclude_assistant')
            AND NOT EXISTS (SELECT 1 FROM authorizations a WHERE a.element_id = e.id AND {active} AND NOT a.allow_assistant)"""
    }
    for name, query in views.items():
        op.execute(sa.text(f"CREATE VIEW {name} AS {query}"))
    if connection.dialect.name == "postgresql":
        op.execute(sa.text("CREATE INDEX ix_fragment_embedding ON index_fragments USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"))
        op.execute(sa.text("CREATE INDEX ix_fragment_search ON index_fragments USING gin (search_vector)"))


def downgrade():
    raise RuntimeError("Normalized content migration preserves legacy evidence; restore a verified backup to roll back")
