from sqlalchemy import ForeignKey, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.managed_campaign import ManagedCampaign


class CampaignOperation(Base):
    __tablename__ = "campaign_operations"

    operation_id: Mapped[str] = mapped_column(primary_key=True)
    managed_id: Mapped[int] = mapped_column(ForeignKey("managed_campaigns.id"), unique=True)
    campaign: Mapped[ManagedCampaign] = relationship()
    alias: Mapped[str] = mapped_column(unique=True)
    name_prefix: Mapped[str]
    geo: Mapped[str]
    offer_id: Mapped[int]
    regular_stream_id: Mapped[int | None]
    default_stream_id: Mapped[int | None]
    stage: Mapped[str]
    error_code: Mapped[str | None]
    outcome_unknown: Mapped[bool] = mapped_column(server_default=false())
