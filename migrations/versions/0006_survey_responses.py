"""Anonymous answers to the closing conversation's opinion questions (owner decision D6).

Only the day is kept, never the time, so a response cannot be matched with a visit by its timestamp.
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "survey_responses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("survey_key", sa.String(100), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("answered_on", sa.Date(), nullable=False),
        sa.Column("platform", sa.String(10)),
        sa.Column("answers", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("survey_responses")
