"""Track individual campaign creation attempts."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_operations",
        sa.Column("operation_id", sa.String(), primary_key=True),
        sa.Column("managed_id", sa.Integer(), sa.ForeignKey("managed_campaigns.id"),
                  nullable=False, unique=True),
        sa.Column("alias", sa.String(), nullable=False, unique=True),
        sa.Column("name_prefix", sa.String(), nullable=False),
        sa.Column("geo", sa.String(), nullable=False),
        sa.Column("offer_id", sa.Integer(), nullable=False),
        sa.Column("regular_stream_id", sa.Integer()),
        sa.Column("default_stream_id", sa.Integer()),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("error_code", sa.String()),
        sa.Column("outcome_unknown", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_table("campaign_operations")
