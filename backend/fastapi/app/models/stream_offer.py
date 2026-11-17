"""One offer position in a Keitaro stream, including archived positions (A-04).

A Keitaro offer is a global entity. This row is a local *position* in a
specific stream; multiple positions may legitimately reference one offer ID.
Remote observations are kept separately from local editor state, so removing a
position from a draft never means deleting the global Keitaro offer.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.stream import Stream


LOCAL_OFFER_ACTIVE = "ACTIVE"
LOCAL_OFFER_REMOVED = "REMOVED"


class StreamOffer(Base):
    """A stable, locally identified offer position; never a global offer row."""

    __tablename__ = "kt_stream_offers"
    __table_args__ = (
        CheckConstraint(
            "keitaro_offer_id > 0",
            name="ck_kt_stream_offers_offer_id_positive",
        ),
        CheckConstraint(
            "keitaro_relation_id IS NULL OR keitaro_relation_id > 0",
            name="ck_kt_stream_offers_relation_id_positive",
        ),
        CheckConstraint(
            "remote_share IS NULL OR remote_share >= 0",
            name="ck_kt_stream_offers_remote_share_nonnegative",
        ),
        CheckConstraint(
            "local_status IN ('ACTIVE', 'REMOVED')",
            name="ck_kt_stream_offers_local_status",
        ),
        CheckConstraint(
            "pinned_percent IS NULL OR pinned_percent BETWEEN 0 AND 100",
            name="ck_kt_stream_offers_pinned_percent_range",
        ),
        Index("ix_kt_stream_offers_stream_status", "stream_id", "local_status"),
        Index("ix_kt_stream_offers_stream_offer", "stream_id", "keitaro_offer_id"),
    )

    id: Mapped[PythonUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    stream_id: Mapped[PythonUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kt_streams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    keitaro_offer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # A remote stream/offer association identifier may not be provided by KT.
    keitaro_relation_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    # Raw observed remote share/weight, without a lossy fixed decimal scale.
    # This is NOT the calculated editor percentage.
    remote_share: Mapped[Decimal | None] = mapped_column(
        Numeric(), nullable=True
    )
    # Preserve Keitaro's reported state without inventing an enum for its API.
    remote_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    local_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=LOCAL_OFFER_ACTIVE,
        server_default=text("'ACTIVE'"),
    )
    # PIN is local edit state; nothing here automatically writes to Keitaro.
    pinned_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    stream: Mapped[Stream] = relationship(back_populates="offer_positions")
