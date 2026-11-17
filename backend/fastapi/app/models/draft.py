"""One unpublished editor workspace per stream, based on immutable snapshots.

Draft statuses and revision are stored here; applying editor actions and status
transitions is a later service-layer step. No operation in this model writes to
Keitaro. created_by_id is a Django user ID, not a cross-ORM foreign key.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.stream import Stream


DRAFT_SYNCED = "SYNCED"
DRAFT_DIRTY = "DIRTY"
DRAFT_CONFLICT = "CONFLICT"
DRAFT_PUBLISHING = "PUBLISHING"
DRAFT_VERIFYING = "VERIFYING"
DRAFT_UNCERTAIN = "UNCERTAIN"
DRAFT_FAILED = "FAILED"
DRAFT_PUBLISHED = "PUBLISHED"
DRAFT_CANCELLED = "CANCELLED"

DRAFT_STATUSES = frozenset(
    {
        DRAFT_SYNCED,
        DRAFT_DIRTY,
        DRAFT_CONFLICT,
        DRAFT_PUBLISHING,
        DRAFT_VERIFYING,
        DRAFT_UNCERTAIN,
        DRAFT_FAILED,
        DRAFT_PUBLISHED,
        DRAFT_CANCELLED,
    }
)
TERMINAL_DRAFT_STATUSES = frozenset({DRAFT_PUBLISHED, DRAFT_CANCELLED})


class StreamDraft(Base):
    """Draft metadata; draft offer rows will be added in a separate migration."""

    __tablename__ = "kt_stream_drafts"
    __table_args__ = (
        # Composite FKs prevent a draft of stream A from using snapshots of B.
        ForeignKeyConstraint(
            ["base_snapshot_id", "stream_id"],
            ["kt_stream_snapshots.id", "kt_stream_snapshots.stream_id"],
            ondelete="RESTRICT",
            name="fk_kt_stream_drafts_base_snapshot_stream",
        ),
        ForeignKeyConstraint(
            ["latest_remote_snapshot_id", "stream_id"],
            ["kt_stream_snapshots.id", "kt_stream_snapshots.stream_id"],
            ondelete="RESTRICT",
            name="fk_kt_stream_drafts_latest_snapshot_stream",
        ),
        CheckConstraint(
            "status IN ('SYNCED', 'DIRTY', 'CONFLICT', 'PUBLISHING', "
            "'VERIFYING', 'UNCERTAIN', 'FAILED', 'PUBLISHED', 'CANCELLED')",
            name="ck_kt_stream_drafts_status",
        ),
        CheckConstraint(
            "revision >= 0", name="ck_kt_stream_drafts_revision_nonnegative"
        ),
        CheckConstraint(
            "created_by_id > 0", name="ck_kt_stream_drafts_creator_positive"
        ),
        CheckConstraint(
            "conflict_details IS NULL OR "
            "jsonb_typeof(conflict_details) = 'object'",
            name="ck_kt_stream_drafts_conflict_object",
        ),
        # FAILED/UNCERTAIN also block a new draft until reconciled explicitly.
        Index(
            "uq_kt_stream_drafts_one_unfinished_per_stream",
            "stream_id",
            unique=True,
            postgresql_where=text("status NOT IN ('PUBLISHED', 'CANCELLED')"),
        ),
        Index("ix_kt_stream_drafts_stream_created", "stream_id", "created_at"),
    )

    id: Mapped[PythonUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    stream_id: Mapped[PythonUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kt_streams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    base_snapshot_id: Mapped[PythonUUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    latest_remote_snapshot_id: Mapped[PythonUUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DRAFT_SYNCED,
        server_default=text("'SYNCED'"),
    )
    # Future write services must perform compare-and-swap on this revision.
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Django's accounts_user owns this identifier; avoid a cross-ORM FK.
    created_by_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conflict_details: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
    )

    stream: Mapped[Stream] = relationship(back_populates="drafts")
