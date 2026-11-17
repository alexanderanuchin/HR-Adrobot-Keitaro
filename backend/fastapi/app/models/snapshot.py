"""Append-only normalized stream snapshots used for A-02 conflict detection.

Keitaro response normalization belongs to the integration layer. This model only
accepts already normalized JSON objects and computes stable SHA-256 fingerprints.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, event, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.stream import Stream


def canonical_snapshot_json(payload: dict[str, object]) -> str:
    """Serialize a normalized stream object with deterministic object-key order."""
    if not isinstance(payload, dict):
        raise TypeError("normalized snapshot payload must be a JSON object")

    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def snapshot_fingerprint(payload: dict[str, object]) -> str:
    """Hash the UTF-8 canonical JSON, not Python's nondeterministic hash()."""
    return hashlib.sha256(canonical_snapshot_json(payload).encode("utf-8")).hexdigest()


class StreamSnapshot(Base):
    __tablename__ = "kt_stream_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "id", "stream_id", name="uq_kt_stream_snapshots_id_stream"
        ),
        CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_kt_stream_snapshots_fingerprint_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object'",
            name="ck_kt_stream_snapshots_payload_object",
        ),
        Index("ix_kt_stream_snapshots_stream_captured", "stream_id", "captured_at"),
    )

    id: Mapped[PythonUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    stream_id: Mapped[PythonUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kt_streams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    normalized_payload: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )

    stream: Mapped[Stream] = relationship(back_populates="snapshots")

    @classmethod
    def capture(
        cls, *, stream_id: PythonUUID, normalized_payload: dict[str, object]
    ) -> StreamSnapshot:
        """Prepare a snapshot; copy the payload so callers cannot mutate it later."""
        canonical = canonical_snapshot_json(normalized_payload)
        copied_payload = json.loads(canonical)
        return cls(
            stream_id=stream_id,
            normalized_payload=copied_payload,
            fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


@event.listens_for(StreamSnapshot, "before_insert")
def _validate_snapshot_fingerprint(_mapper, _connection, target: StreamSnapshot) -> None:
    """Reject inconsistent fingerprints from any SQLAlchemy ORM insertion path."""
    if target.fingerprint != snapshot_fingerprint(target.normalized_payload):
        raise ValueError("stream snapshot fingerprint does not match its payload")
