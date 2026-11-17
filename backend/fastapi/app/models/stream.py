"""Local reference to a Keitaro campaign stream (A-04)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.campaign import Campaign
    from app.models.snapshot import StreamSnapshot
    from app.models.stream_offer import StreamOffer


class Stream(Base):
    __tablename__ = "kt_streams"
    __table_args__ = (
        CheckConstraint(
            "keitaro_stream_id > 0",
            name="ck_kt_streams_keitaro_stream_id_positive",
        ),
        UniqueConstraint(
            "keitaro_stream_id",
            name="uq_kt_streams_keitaro_stream_id",
        ),
        Index("ix_kt_streams_campaign_id", "campaign_id"),
    )

    id: Mapped[PythonUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    campaign_id: Mapped[PythonUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kt_campaigns.id", ondelete="RESTRICT"),
        nullable=False,
    )
    keitaro_stream_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    stream_type: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_type: Mapped[str] = mapped_column(String(32), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    campaign: Mapped[Campaign] = relationship(back_populates="streams")
    snapshots: Mapped[list[StreamSnapshot]] = relationship(
        back_populates="stream", passive_deletes=True
    )
    offer_positions: Mapped[list[StreamOffer]] = relationship(
        back_populates="stream", passive_deletes=True
    )
