"""Transactional ADD and REMOVE for unpublished Keitaro stream drafts.

These operations modify only ``kt_draft_offers`` and ``kt_stream_drafts``.
They never call Keitaro, alter a global offer or change ``kt_stream_offers``.

Caller contract: invoke inside a SQLAlchemy transaction (e.g.
``with Session(engine).begin(): ...``) and roll it back on any exception.
We flush but NEVER commit. The same stream-row lock order as draft_workspace
serializes these edits with future FETCH/sync writers; a draft-row lock and a
revision compare-and-swap protect against competing editor requests.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import DraftOffer, Stream, StreamDraft
from app.models.draft import DRAFT_DIRTY, DRAFT_SYNCED
from app.models.draft_offer import DRAFT_OFFER_ACTIVE, DRAFT_OFFER_REMOVED
from app.services.draft_workspace import DraftWorkspaceView, read_draft_workspace
from app.services.weight_distribution import (
    WeightDistributionError,
    WeightPosition,
    distribute_weights,
)


EDITABLE_STATUSES = frozenset({DRAFT_SYNCED, DRAFT_DIRTY})
MAX_POSTGRES_INT = 2_147_483_647
MAX_POSTGRES_BIGINT = 9_223_372_036_854_775_807


class DraftEditError(ValueError):
    """The requested local edit is invalid; do not publish anything."""


class DraftEditNotFound(LookupError):
    """The draft or the requested local offer position does not exist."""


class DraftRevisionConflict(DraftEditError):
    """The editor's revision is stale; reload the workspace before retrying."""


class DraftEditBlocked(DraftEditError):
    """Draft is in a state where local edits must be blocked."""


def _validate_request(
    draft_id: UUID, expected_revision: int, *, position_id: UUID | None = None
) -> None:
    if not isinstance(draft_id, UUID):
        raise DraftEditError("draft_id must be a UUID")
    if type(expected_revision) is not int or not 0 <= expected_revision < MAX_POSTGRES_INT:
        raise DraftEditError("expected_revision must be a non-negative PostgreSQL integer")
    if position_id is not None and not isinstance(position_id, UUID):
        raise DraftEditError("position_id must be a UUID")


def _locked_draft(session: Session, draft_id: UUID, expected_revision: int) -> StreamDraft:
    # Lock order matches the workspace creator: stream, then draft. Read only
    # the stream identifier first to avoid locking in the opposite order.
    stream_id = session.scalar(select(StreamDraft.stream_id).where(StreamDraft.id == draft_id))
    if stream_id is None:
        raise DraftEditNotFound("draft not found")
    locked_stream = session.scalar(
        select(Stream).where(Stream.id == stream_id).with_for_update()
    )
    if locked_stream is None:
        raise DraftEditNotFound("draft stream not found")
    if locked_stream.schema_type != "offers":
        raise DraftEditBlocked("only offer-based streams may be edited")

    draft = session.scalar(
        select(StreamDraft).where(StreamDraft.id == draft_id).with_for_update()
    )
    if draft is None:
        raise DraftEditNotFound("draft not found")
    if draft.stream_id != stream_id:
        raise DraftEditBlocked("draft stream changed during locking")
    if draft.revision != expected_revision:
        raise DraftRevisionConflict("stale draft revision; reload before editing")
    if draft.status not in EDITABLE_STATUSES:
        raise DraftEditBlocked(f"draft status {draft.status} does not permit editing")
    return draft


def _draft_positions(session: Session, draft_id: UUID) -> list[DraftOffer]:
    return list(
        session.scalars(
            select(DraftOffer)
            .where(DraftOffer.draft_id == draft_id)
            .order_by(DraftOffer.position_index, DraftOffer.id)
        ).all()
    )


def _calculate(
    rows: list[DraftOffer], *,
    removed_id: UUID | None = None,
    restored_id: UUID | None = None,
    pin_override: tuple[UUID, int | None] | None = None,
) -> dict[UUID, int]:
    """Validate a hypothetical edit before changing any persistent position."""
    proposal = [
        WeightPosition(
            position_id=row.id,
            position_index=row.position_index,
            status=(
                DRAFT_OFFER_REMOVED if row.id == removed_id
                else DRAFT_OFFER_ACTIVE if row.id == restored_id
                else row.status
            ),
            pinned_percent=(
                pin_override[1]
                if pin_override is not None and row.id == pin_override[0]
                else row.pinned_percent
            ),
        )
        for row in rows
    ]
    try:
        weights = distribute_weights(proposal)
    except WeightDistributionError as exc:
        raise DraftEditError(str(exc)) from exc
    return {weight.position_id: weight.calculated_percent for weight in weights}


def _save_edit(
    session: Session,
    *,
    draft: StreamDraft,
    expected_revision: int,
    rows: list[DraftOffer],
    calculated: dict[UUID, int],
    added: DraftOffer | None = None,
    removed_id: UUID | None = None,
    restored_id: UUID | None = None,
    pin_override: tuple[UUID, int | None] | None = None,
) -> DraftWorkspaceView:
    # Database compare-and-swap is a second line of defence in addition to
    # SELECT FOR UPDATE; no stale caller can silently overwrite newer edits.
    next_revision = session.scalar(
        update(StreamDraft)
        .where(StreamDraft.id == draft.id)
        .where(StreamDraft.revision == expected_revision)
        .where(StreamDraft.status.in_(EDITABLE_STATUSES))
        .values(
            revision=StreamDraft.revision + 1,
            status=DRAFT_DIRTY,
            updated_at=func.clock_timestamp(),
        )
        .returning(StreamDraft.revision)
    )
    if next_revision is None:
        raise DraftRevisionConflict("draft changed during editing; reload")

    if added is not None:
        session.add(added)
    for row in rows:
        if row.id == removed_id:
            row.status = DRAFT_OFFER_REMOVED
        elif row.id == restored_id:
            row.status = DRAFT_OFFER_ACTIVE
        if pin_override is not None and row.id == pin_override[0]:
            row.pinned_percent = pin_override[1]
        row.calculated_percent = calculated[row.id]
    session.flush()
    session.refresh(draft)  # Read the DB value after compare-and-swap, not a stale ORM cache.
    return read_draft_workspace(session, draft.id)


def add_offer_to_draft(
    session: Session,
    *,
    draft_id: UUID,
    expected_revision: int,
    keitaro_offer_id: int,
) -> DraftWorkspaceView:
    """ADD one NEW local position, even if that global offer ID already exists.

    No remote lookup is performed: the future authenticated API must verify the
    global offer ID through a read-only Keitaro adapter before invoking this.
    """
    _validate_request(draft_id, expected_revision)
    if (
        type(keitaro_offer_id) is not int
        or not 0 < keitaro_offer_id <= MAX_POSTGRES_BIGINT
    ):
        raise DraftEditError("keitaro_offer_id must be a positive 64-bit integer")

    draft = _locked_draft(session, draft_id, expected_revision)
    rows = _draft_positions(session, draft.id)
    next_index = max((row.position_index for row in rows), default=-1) + 1
    if next_index > MAX_POSTGRES_INT:
        raise DraftEditError("draft has too many offer positions")
    added = DraftOffer(
        id=uuid4(),
        draft_id=draft.id,
        stream_id=draft.stream_id,
        source_position_id=None,
        keitaro_offer_id=keitaro_offer_id,
        position_index=next_index,
        status=DRAFT_OFFER_ACTIVE,
        pinned_percent=None,
        calculated_percent=0,
    )
    rows.append(added)
    calculated = _calculate(rows)
    return _save_edit(
        session,
        draft=draft,
        expected_revision=expected_revision,
        rows=rows,
        calculated=calculated,
        added=added,
    )


def remove_offer_from_draft(
    session: Session,
    *,
    draft_id: UUID,
    expected_revision: int,
    position_id: UUID,
) -> DraftWorkspaceView:
    """REMOVE exactly one local position by UUID; archive it with 0 percent.

    Its global Keitaro offer, remote association, source position and saved PIN
    are untouched. The local row remains available for future BRING BACK.
    """
    _validate_request(draft_id, expected_revision)
    if not isinstance(position_id, UUID):
        raise DraftEditError("position_id must be a UUID")

    draft = _locked_draft(session, draft_id, expected_revision)
    rows = _draft_positions(session, draft.id)
    row = next((row for row in rows if row.id == position_id), None)
    if row is None:
        raise DraftEditNotFound("offer position not found in this draft")
    if row.status == DRAFT_OFFER_REMOVED:
        raise DraftEditError("offer position is already removed")
    if row.status != DRAFT_OFFER_ACTIVE:
        raise DraftEditBlocked("offer position is not active")

    calculated = _calculate(rows, removed_id=position_id)
    return _save_edit(
        session,
        draft=draft,
        expected_revision=expected_revision,
        rows=rows,
        calculated=calculated,
        removed_id=position_id,
    )


def bring_back_offer_in_draft(
    session: Session,
    *,
    draft_id: UUID,
    expected_revision: int,
    position_id: UUID,
) -> DraftWorkspaceView:
    """Restore an archived local position, preserving its UUID, source and PIN.

    Only the draft changes. A saved PIN is reactivated and checked against the
    full draft allocation; an impossible restore fails *before* any write.
    """
    _validate_request(draft_id, expected_revision)
    if not isinstance(position_id, UUID):
        raise DraftEditError("position_id must be a UUID")

    draft = _locked_draft(session, draft_id, expected_revision)
    rows = _draft_positions(session, draft.id)
    row = next((item for item in rows if item.id == position_id), None)
    if row is None:
        raise DraftEditNotFound("offer position not found in this draft")
    if row.status == DRAFT_OFFER_ACTIVE:
        raise DraftEditError("offer position is already active")
    if row.status != DRAFT_OFFER_REMOVED:
        raise DraftEditBlocked("offer position is not removed")

    calculated = _calculate(rows, restored_id=position_id)
    return _save_edit(
        session,
        draft=draft,
        expected_revision=expected_revision,
        rows=rows,
        calculated=calculated,
        restored_id=position_id,
    )


def pin_offer_in_draft(
    session: Session,
    *,
    draft_id: UUID,
    expected_revision: int,
    position_id: UUID,
    pinned_percent: int | None,
) -> DraftWorkspaceView:
    """PIN to an integer 0..100, or UNPIN with None, on an ACTIVE position.

    Refuse no-op writes so a repeated request does not advance the revision.
    Weight recalculation and revision update occur in the caller's transaction.
    """
    _validate_request(draft_id, expected_revision)
    if not isinstance(position_id, UUID):
        raise DraftEditError("position_id must be a UUID")
    if pinned_percent is not None and (
        type(pinned_percent) is not int or not 0 <= pinned_percent <= 100
    ):
        raise DraftEditError("pinned_percent must be an integer from 0 to 100 or None")

    draft = _locked_draft(session, draft_id, expected_revision)
    rows = _draft_positions(session, draft.id)
    row = next((item for item in rows if item.id == position_id), None)
    if row is None:
        raise DraftEditNotFound("offer position not found in this draft")
    if row.status != DRAFT_OFFER_ACTIVE:
        raise DraftEditBlocked("only active offer positions can be pinned")
    if row.pinned_percent == pinned_percent:
        raise DraftEditError("offer position already has the requested PIN")

    override = (position_id, pinned_percent)
    calculated = _calculate(rows, pin_override=override)
    return _save_edit(
        session,
        draft=draft,
        expected_revision=expected_revision,
        rows=rows,
        calculated=calculated,
        pin_override=override,
    )
