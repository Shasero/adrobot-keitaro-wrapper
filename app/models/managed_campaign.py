from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ManagedCampaign(Base):
    __tablename__ = "managed_campaigns"
    __table_args__ = (
        UniqueConstraint("remote_campaign_id", name="uq_managed_campaigns_remote_campaign_id"),
        CheckConstraint("length(trim(name)) > 0", name="ck_managed_campaigns_name_nonblank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    remote_campaign_id: Mapped[int | None]
    name: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), server_default=func.current_timestamp(), onupdate=func.current_timestamp()
    )
