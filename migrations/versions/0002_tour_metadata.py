"""T-14: published tours and visitor names without changing stable anchor keys."""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tours",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("name", sa.String(250), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.DateTime(timezone=True)),
        sa.Column("guide_key", sa.String(100)),
        sa.Column("guide_name", sa.String(250)),
        sa.Column("guide_room_key", sa.String(100)),
        sa.Column("guide_available", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("rooms", sa.Column("name", sa.String(250), nullable=False, server_default=""))
    op.add_column("rooms", sa.Column("short_description", sa.Text(), nullable=False, server_default=""))
    op.add_column("points", sa.Column("name", sa.String(250), nullable=False, server_default=""))
    op.add_column("points", sa.Column("activation", sa.JSON(), nullable=False, server_default='["keyboard"]'))
    connection = op.get_bind()
    room_table = sa.table("rooms", sa.column("tour_id"))
    tour_table = sa.table("tours", sa.column("id"), sa.column("name"), sa.column("published"))
    for key in connection.scalars(sa.select(room_table.c.tour_id).distinct()):
        connection.execute(tour_table.insert().values(id=key, name=key, published=True))


def downgrade():
    for column in ("activation", "name"):
        op.drop_column("points", column)
    for column in ("short_description", "name"):
        op.drop_column("rooms", column)
    op.drop_table("tours")
