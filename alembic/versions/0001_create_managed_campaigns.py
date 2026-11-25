"""Create managed campaigns."""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("remote_campaign_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("remote_campaign_id", name="uq_managed_campaigns_remote_campaign_id"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_managed_campaigns_name_nonblank"),
    )


def downgrade() -> None:
    op.drop_table("managed_campaigns")
