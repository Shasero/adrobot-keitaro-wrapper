"""Keep versioned offer drafts independently of managed campaigns."""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "offer_drafts",
        sa.Column("campaign_id", sa.Integer(), primary_key=True),
        sa.Column("stream_id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("offer_drafts")
