from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OfferDraft(Base):
    __tablename__ = "offer_drafts"

    campaign_id: Mapped[int] = mapped_column(primary_key=True)
    stream_id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int]
    document: Mapped[dict] = mapped_column(JSON)
