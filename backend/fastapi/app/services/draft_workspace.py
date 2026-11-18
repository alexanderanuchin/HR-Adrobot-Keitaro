"""Open or read a local offer-editor workspace (A-01/A-02/A-04).

The Keitaro integration must first normalize a captured stream and supply
*verified* integer initial percentages for every tracked source position.
Remote ``remote_share`` units are intentionally NOT interpreted here.

The caller owns the SQLAlchemy transaction (``with Session.begin(): ...``).
This service flushes, but never commits, and never calls Keitaro. To make
concurrent creation safe, all writers that update a stream's observed offers
must also lock the same ``kt_streams`` row before changing those offers.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DraftOffer, Stream, StreamDraft, StreamOffer, StreamSnapshot
from app.models.draft import TERMINAL_DRAFT_STATUSES
from app.models.draft_offer import DRAFT_OFFER_ACTIVE, DRAFT_OFFER_REMOVED


class DraftWorkspaceError(ValueError):
    """The requested workspace cannot be created from verified local data."""


class DraftWorkspaceNotFound(LookupError):
    """The requested stream or draft does not exist."""


@dataclass(frozen=True, slots=True)
class InitialOfferPosition:
    """Trusted snapshot-derived ordering and integer percentage for a source row."""

    source_position_id: UUID
    position_index: int
    calculated_percent: int


@dataclass(frozen=True, slots=True)
class DraftOfferView:
    position_id: UUID
    source_position_id: UUID | None
    keitaro_offer_id: int
    position_index: int
    status: str
    pinned_percent: int | None
    pin_immediately: bool
    pending_pinned_percent: int | None
    is_deleted: bool
    calculated_percent: int


@dataclass(frozen=True, slots=True)
class DraftWorkspaceView:
    draft_id: UUID
    stream_id: UUID
    base_snapshot_id: UUID
    latest_remote_snapshot_id: UUID | None
    status: str
    revision: int
    created_by_id: int
    positions: tuple[DraftOfferView, ...]


def read_draft_workspace(session: Session, draft_id: UUID) -> DraftWorkspaceView:
    """Read the persisted workspace in display order, without changing it."""
    if not isinstance(draft_id, UUID):
        raise DraftWorkspaceError("draft_id must be a UUID")
    draft = session.get(StreamDraft, draft_id)
    if draft is None:
        raise DraftWorkspaceNotFound("draft not found")

    offers = session.scalars(
        select(DraftOffer)
        .where(DraftOffer.draft_id == draft_id)
        .order_by(DraftOffer.position_index, DraftOffer.id)
    ).all()
    return DraftWorkspaceView(
        draft_id=draft.id,
        stream_id=draft.stream_id,
        base_snapshot_id=draft.base_snapshot_id,
        latest_remote_snapshot_id=draft.latest_remote_snapshot_id,
        status=draft.status,
        revision=draft.revision,
        created_by_id=draft.created_by_id,
        positions=tuple(
            DraftOfferView(
                position_id=offer.id,
                source_position_id=offer.source_position_id,
                keitaro_offer_id=offer.keitaro_offer_id,
                position_index=offer.position_index,
                status=offer.status,
                pinned_percent=offer.pinned_percent,
                pin_immediately=offer.pin_immediately,
                pending_pinned_percent=offer.pending_pinned_percent,
                is_deleted=offer.is_deleted,
                calculated_percent=offer.calculated_percent,
            )
            for offer in offers
        ),
    )


def _validate_initial_positions(
    initial_positions: tuple[InitialOfferPosition, ...],
    source_rows: list[StreamOffer],
) -> None:
    """Reject partial, swapped or speculative initial data before any write."""
    if any(not isinstance(row, InitialOfferPosition) for row in initial_positions):
        raise DraftWorkspaceError("initial positions must be InitialOfferPosition records")

    by_id = {row.id: row for row in source_rows}
    seen: set[UUID] = set()
    indices: set[int] = set()
    active_total = 0
    active_count = sum(row.local_status == DRAFT_OFFER_ACTIVE for row in source_rows)
    expected_total = 100
    for position in initial_positions:
        if not isinstance(position.source_position_id, UUID):
            raise DraftWorkspaceError("source_position_id must be a UUID")
        source = by_id.get(position.source_position_id)
        if source is None:
            raise DraftWorkspaceError("initial position refers to an unknown stream offer")
        if position.source_position_id in seen:
            raise DraftWorkspaceError("same source position appears twice")
        if type(position.position_index) is not int or position.position_index < 0:
            raise DraftWorkspaceError("position_index must be a non-negative integer")
        if position.position_index in indices:
            raise DraftWorkspaceError("duplicate position_index")
        if type(position.calculated_percent) is not int or not 0 <= position.calculated_percent <= 100:
            raise DraftWorkspaceError("calculated_percent must be an integer in 0..100")
        if source.local_status not in (DRAFT_OFFER_ACTIVE, DRAFT_OFFER_REMOVED):
            raise DraftWorkspaceError("unknown source position status")
        if source.local_status == DRAFT_OFFER_REMOVED:
            if position.calculated_percent != 0:
                raise DraftWorkspaceError("removed initial position must display zero")
        else:
            active_total += position.calculated_percent
            if source.pinned_percent is not None:
                if active_count == 1 and source.pin_immediately:
                    expected_total = source.pinned_percent
                if (active_count > 1 or source.pin_immediately) and source.pinned_percent != position.calculated_percent:
                    raise DraftWorkspaceError("initial percent conflicts with the saved PIN")
        seen.add(position.source_position_id)
        indices.add(position.position_index)

    if seen != set(by_id):
        raise DraftWorkspaceError("initial positions must include every tracked offer, including archive")
    if indices != set(range(len(initial_positions))):
        raise DraftWorkspaceError("initial indices must be consecutive from zero")
    if active_count and active_total != expected_total:
        raise DraftWorkspaceError(f"active initial percentages must sum to {expected_total}")
    # For no active positions, all values have already been checked to be zero.


def open_or_create_draft_workspace(
    session: Session,
    *,
    stream_id: UUID,
    base_snapshot_id: UUID,
    created_by_id: int,
    initial_positions: tuple[InitialOfferPosition, ...],
) -> DraftWorkspaceView:
    """Return the unfinished draft, or create one from verified observed rows.

    The stream lock serializes concurrent creators and prevents two unfinished
    drafts. Existing DIRTY/CONFLICT/UNCERTAIN/FAILED drafts are never replaced.
    For a new draft, the base must be the latest stored snapshot of this stream.
    Any mismatching or incomplete initial state is rejected before writing.

    Do not call ``session.commit()`` here. On exception, the caller must roll
    back the enclosing transaction; on success, the caller must commit it.
    """
    if not isinstance(stream_id, UUID) or not isinstance(base_snapshot_id, UUID):
        raise DraftWorkspaceError("stream_id and base_snapshot_id must be UUIDs")
    if type(created_by_id) is not int or created_by_id <= 0:
        raise DraftWorkspaceError("created_by_id must be a positive integer")

    # PostgreSQL row lock: all creators of a stream serialize on this row.
    stream = session.scalar(
        select(Stream).where(Stream.id == stream_id).with_for_update()
    )
    if stream is None:
        raise DraftWorkspaceNotFound("stream not found")
    # This service edits offer positions, not Google/redirect-only streams.
    # The Keitaro adapter must normalize offer streams to schema_type="offers".
    if stream.schema_type != "offers":
        raise DraftWorkspaceError("stream is not an offers-based stream")

    existing = session.scalar(
        select(StreamDraft)
        .where(StreamDraft.stream_id == stream_id)
        .where(StreamDraft.status.not_in(TERMINAL_DRAFT_STATUSES))
    )
    if existing is not None:
        # Never overwrite user changes on repeated opens or FETCH.
        return read_draft_workspace(session, existing.id)

    if not isinstance(initial_positions, tuple):
        raise DraftWorkspaceError("initial_positions must be a tuple")

    latest_snapshot = session.scalar(
        select(StreamSnapshot)
        .where(StreamSnapshot.stream_id == stream_id)
        .order_by(StreamSnapshot.captured_at.desc(), StreamSnapshot.id.desc())
        .limit(1)
    )
    if latest_snapshot is None:
        raise DraftWorkspaceError("stream needs a captured snapshot before editing")
    if latest_snapshot.id != base_snapshot_id:
        raise DraftWorkspaceError("base_snapshot_id is not the latest captured snapshot")

    source_rows = session.scalars(
        select(StreamOffer).where(StreamOffer.stream_id == stream_id, StreamOffer.is_deleted.is_(False))
    ).all()
    _validate_initial_positions(initial_positions, source_rows)

    draft = StreamDraft(
        stream_id=stream_id,
        base_snapshot_id=base_snapshot_id,
        created_by_id=created_by_id,
    )
    session.add(draft)
    session.flush()

    by_id = {source.id: source for source in source_rows}
    session.add_all(
        DraftOffer(
            draft_id=draft.id,
            stream_id=stream_id,
            source_position_id=position.source_position_id,
            keitaro_offer_id=by_id[position.source_position_id].keitaro_offer_id,
            position_index=position.position_index,
            status=by_id[position.source_position_id].local_status,
            pinned_percent=by_id[position.source_position_id].pinned_percent,
            pin_immediately=by_id[position.source_position_id].pin_immediately,
            pending_pinned_percent=by_id[position.source_position_id].pending_pinned_percent,
            calculated_percent=position.calculated_percent,
        )
        for position in initial_positions
    )
    session.flush()
    return read_draft_workspace(session, draft.id)
