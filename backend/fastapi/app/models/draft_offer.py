"""An unpublished offer position inside a stream editor draft (A-02/A-04).

Rows here never mutate the global Keitaro offer or the published stream offer.
Each draft row is a separate position, even if offer IDs repeat. Calculated
percentages are draft previews, not Keitaro's observed remote shares.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.draft import StreamDraft


DRAFT_OFFER_ACTIVE = "ACTIVE"
DRAFT_OFFER_REMOVED = "REMOVED"


class DraftOffer(Base):
    """Stable draft position, optionally anchored to a published position."""

    __tablename__ = "kt_draft_offers"
    __table_args__ = (
        # Stream ownership is checked in PostgreSQL for both foreign keys.
        ForeignKeyConstraint(
            ["draft_id", "stream_id"],
            ["kt_stream_drafts.id", "kt_stream_drafts.stream_id"],
            ondelete="RESTRICT",
            name="fk_kt_draft_offers_draft_stream",
        ),
        ForeignKeyConstraint(
            ["source_position_id", "stream_id", "keitaro_offer_id"],
            ["kt_stream_offers.id", "kt_stream_offers.stream_id", "kt_stream_offers.keitaro_offer_id"],
            ondelete="RESTRICT",
            name="fk_kt_draft_offers_source_position_identity",
        ),
        UniqueConstraint("draft_id", "position_index", name="uq_kt_draft_offers_position"),
        UniqueConstraint("draft_id", "source_position_id", name="uq_kt_draft_offers_source"),
        CheckConstraint("position_index >= 0", name="ck_kt_draft_offers_position_nonnegative"),
        CheckConstraint("keitaro_offer_id > 0", name="ck_kt_draft_offers_offer_id_positive"),
        CheckConstraint(
            "status IN ('ACTIVE', 'REMOVED')", name="ck_kt_draft_offers_status"
        ),
        CheckConstraint(
            "pinned_percent IS NULL OR pinned_percent BETWEEN 0 AND 100",
            name="ck_kt_draft_offers_pinned_range",
        ),
        CheckConstraint(
            "pending_pinned_percent IS NULL OR pending_pinned_percent BETWEEN 0 AND 100",
            name="ck_kt_draft_offers_pending_pin_range",
        ),
        CheckConstraint(
            "calculated_percent BETWEEN 0 AND 100",
            name="ck_kt_draft_offers_calculated_range",
        ),
        CheckConstraint(
            "status <> 'REMOVED' OR calculated_percent = 0",
            name="ck_kt_draft_offers_removed_zero",
        ),
        Index("ix_kt_draft_offers_draft_status", "draft_id", "status"),
    )

    id: Mapped[PythonUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    draft_id: Mapped[PythonUUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    stream_id: Mapped[PythonUUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # NULL means ADD created a new position; otherwise points to an observed row.
    source_position_id: Mapped[PythonUUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    keitaro_offer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Zero-based display/order index, unique within a single draft.
    position_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False,
        default=DRAFT_OFFER_ACTIVE,
        server_default=text("'ACTIVE'"),
    )
    pinned_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pin_immediately: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    pending_pinned_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calculated_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    draft: Mapped[StreamDraft] = relationship(back_populates="offers")
