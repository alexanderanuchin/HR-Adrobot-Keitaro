"""Local reference to a Keitaro campaign (A-04)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.stream import Stream


class Campaign(Base):
    __tablename__ = "kt_campaigns"
    __table_args__ = (
        CheckConstraint(
            "keitaro_campaign_id > 0",
            name="ck_kt_campaigns_keitaro_campaign_id_positive",
        ),
        UniqueConstraint(
            "keitaro_campaign_id",
            name="uq_kt_campaigns_keitaro_campaign_id",
        ),
    )

    id: Mapped[PythonUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    keitaro_campaign_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    traffic_source_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    streams: Mapped[list[Stream]] = relationship(
        back_populates="campaign", passive_deletes=True
    )
