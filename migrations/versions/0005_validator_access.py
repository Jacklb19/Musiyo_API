"""Hash session identifiers and record bounded login attempts and correction metadata."""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("validator_sessions") as batch:
        batch.alter_column("id", existing_type=sa.String(36), type_=sa.String(64), existing_nullable=False)
    for name, fields in (
        ("validator_login_attempts", [sa.Column("ip_hash", sa.String(64), nullable=False), sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False)]),
        ("validator_corrections", [sa.Column("validator_id", sa.String(100), sa.ForeignKey("validators.id"), nullable=False),
            sa.Column("element_id", sa.String(100), sa.ForeignKey("elements.id"), nullable=False),
            sa.Column("fields", sa.JSON(), nullable=False), sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=False)])):
        op.create_table(name, sa.Column("id", sa.String(36), primary_key=True), *fields,
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()))
    op.create_index("ix_validator_login_ip_time", "validator_login_attempts", ["ip_hash", "occurred_at"])


def downgrade():
    raise RuntimeError("Validator access migration is forward-only")
