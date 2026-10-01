"""T-05: initialize English persistence or preserve and migrate the legacy prototype."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

INITIAL_METADATA = sa.MetaData()
sa.Table('elements', INITIAL_METADATA,
    sa.Column('id', sa.String(100), primary_key=True, nullable=False),
    sa.Column('title', sa.String(250), primary_key=False, nullable=False),
    sa.Column('description', sa.Text(), primary_key=False, nullable=False),
    sa.Column('interpretation', sa.Text(), primary_key=False, nullable=False),
    sa.Column('approval_status', sa.String(20), primary_key=False, nullable=False),
    sa.Column('approved_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.Column('authorized_from', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.Column('authorized_until', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.Column('credits', sa.Text(), primary_key=False, nullable=False),
    sa.Column('sources', sa.Text(), primary_key=False, nullable=False),
    sa.Column('restrictions', sa.Text(), primary_key=False, nullable=False),
)
sa.Table('rooms', INITIAL_METADATA,
    sa.Column('id', sa.String(100), primary_key=True, nullable=False),
    sa.Column('tour_id', sa.String(100), primary_key=False, nullable=False),
    sa.Column('order', sa.Integer(), primary_key=False, nullable=False),
)
sa.Table('points', INITIAL_METADATA,
    sa.Column('id', sa.String(100), primary_key=True, nullable=False),
    sa.Column('room_id', sa.String(100), sa.ForeignKey('rooms.id'), primary_key=False, nullable=False),
    sa.Column('order', sa.Integer(), primary_key=False, nullable=False),
)
sa.Table('resources', INITIAL_METADATA,
    sa.Column('id', sa.String(100), primary_key=True, nullable=False),
    sa.Column('element_id', sa.String(100), sa.ForeignKey('elements.id'), primary_key=False, nullable=False),
    sa.Column('name', sa.String(250), primary_key=False, nullable=False),
    sa.Column('mime', sa.String(100), primary_key=False, nullable=False),
    sa.Column('private_path', sa.Text(), primary_key=False, nullable=False),
    sa.Column('approved', sa.Boolean(), primary_key=False, nullable=False),
    sa.Column('authorized_from', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.Column('authorized_until', sa.DateTime(timezone=True), primary_key=False, nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), primary_key=False, nullable=True),
)
sa.Table('point_elements', INITIAL_METADATA,
    sa.Column('point_id', sa.String(100), sa.ForeignKey('points.id'), primary_key=True, nullable=False),
    sa.Column('element_id', sa.String(100), sa.ForeignKey('elements.id'), primary_key=True, nullable=False),
    sa.Column('order', sa.Integer(), primary_key=False, nullable=False),
)
sa.Index('ix_rooms_tour_id', INITIAL_METADATA.tables['rooms'].c['tour_id'], unique=False)

RENAMES = {'elementos': ('elements', {'titulo': 'title', 'descripcion': 'description', 'interpretacion': 'interpretation', 'estado_aprobacion': 'approval_status', 'aprobado_en': 'approved_at', 'autorizado_desde': 'authorized_from', 'autorizado_hasta': 'authorized_until', 'revocado_en': 'revoked_at', 'creditos': 'credits', 'fuentes': 'sources', 'restricciones': 'restrictions'}), 'recursos': ('resources', {'elemento_id': 'element_id', 'nombre': 'name', 'tipo_mime': 'mime', 'ruta_privada': 'private_path', 'aprobado': 'approved', 'autorizado_desde': 'authorized_from', 'autorizado_hasta': 'authorized_until', 'revocado_en': 'revoked_at'}), 'salas': ('rooms', {'recorrido_id': 'tour_id', 'orden': 'order'}), 'puntos': ('points', {'sala_id': 'room_id', 'orden': 'order'}), 'punto_elementos': ('point_elements', {'punto_id': 'point_id', 'elemento_id': 'element_id', 'orden': 'order'})}


def upgrade():
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    legacy = tables.intersection(RENAMES)
    english = tables.intersection(new for new, columns in RENAMES.values())
    if legacy:
        if legacy != set(RENAMES) or english:
            raise RuntimeError("Incomplete or mixed legacy schema; migration refused without modifying data")
        inspector = sa.inspect(connection)
        for old, (new, columns) in RENAMES.items():
            existing_columns = {column["name"] for column in inspector.get_columns(old)}
            if not set(columns).issubset(existing_columns) or "id" not in existing_columns and old != "punto_elementos":
                raise RuntimeError("Incomplete legacy columns; migration refused without modifying data")
        for old, (new, columns) in RENAMES.items():
            op.rename_table(old, new)
            for previous, current in columns.items():
                op.alter_column(new, previous, new_column_name=current)
        op.execute(sa.text("UPDATE elements SET approval_status = CASE approval_status WHEN 'aprobado' THEN 'approved' WHEN 'borrador' THEN 'draft' ELSE approval_status END"))
        for index in sa.inspect(connection).get_indexes("rooms"):
            if index["name"] == "ix_salas_recorrido_id":
                op.drop_index(index["name"], table_name="rooms")
        op.create_index("ix_rooms_tour_id", "rooms", ["tour_id"])
    elif english:
        raise RuntimeError("Unversioned English schema; inspect it before adopting migrations")
    else:
        INITIAL_METADATA.create_all(connection)
    if connection.dialect.name == "postgresql":
        for extension in ("vector", "unaccent", "pg_trgm"):
            op.execute(sa.text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))


def downgrade():
    raise RuntimeError("Initial data-preserving migration cannot be downgraded automatically; restore a verified backup")
