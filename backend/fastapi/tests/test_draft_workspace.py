"""A-02/A-04: open/load draft without changing published Keitaro state.

Pure validation tests run without PostgreSQL. Transactional tests need the
existing 0006_draft_offers Alembic schema and a PostgreSQL test connection.
"""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Campaign, DraftOffer, Stream, StreamDraft, StreamOffer, StreamSnapshot
from app.models.draft import (
    DRAFT_CONFLICT,
    DRAFT_DIRTY,
    DRAFT_FAILED,
    DRAFT_PUBLISHED,
    DRAFT_PUBLISHING,
    DRAFT_UNCERTAIN,
    DRAFT_VERIFYING,
)
from app.models.draft_offer import DRAFT_OFFER_REMOVED
from app.services.draft_workspace import (
    DraftWorkspaceError,
    DraftWorkspaceNotFound,
    InitialOfferPosition,
    _validate_initial_positions,
    open_or_create_draft_workspace,
    read_draft_workspace,
)


def _remote_id() -> int:
    return (uuid4().int % 9_000_000_000_000) + 1


def _source(*, status: str = "ACTIVE", pin: int | None = None) -> StreamOffer:
    return StreamOffer(
        id=uuid4(),
        stream_id=uuid4(),
        keitaro_offer_id=_remote_id(),
        local_status=status,
        pinned_percent=pin,
    )


def _position(source: StreamOffer, index: int, percent: int) -> InitialOfferPosition:
    return InitialOfferPosition(source.id, index, percent)


def test_valid_initial_distribution_preserves_non_equal_shares():
    first, second = _source(), _source()
    _validate_initial_positions((_position(first, 0, 10), _position(second, 1, 90)), [first, second])


def test_valid_initial_archive_keeps_zero_and_saved_pin():
    first, removed = _source(), _source(status="REMOVED", pin=70)
    _validate_initial_positions((_position(first, 0, 100), _position(removed, 1, 0)), [first, removed])


def test_all_removed_or_no_sources_can_initialize_an_empty_preview():
    removed = _source(status="REMOVED")
    _validate_initial_positions((_position(removed, 0, 0),), [removed])
    _validate_initial_positions((), [])


def test_matching_saved_pin_is_accepted():
    first, second = _source(pin=25), _source()
    _validate_initial_positions((_position(first, 0, 25), _position(second, 1, 75)), [first, second])


@pytest.mark.parametrize(
    "positions,expected",
    [
        (lambda a, b: (_position(a, 0, 100),), "include every tracked"),
        (lambda a, b: (_position(a, 0, 50), _position(a, 1, 50)), "appears twice"),
        (lambda a, b: (_position(a, 0, 50), _position(b, 0, 50)), "duplicate position_index"),
        (lambda a, b: (_position(a, 0, 50), _position(b, 2, 50)), "consecutive"),
        (lambda a, b: (_position(a, 0, 80), _position(b, 1, 10)), "sum to 100"),
        (lambda a, b: (_position(a, 0, -1), _position(b, 1, 101)), "calculated_percent"),
        (lambda a, b: (_position(a, True, 50), _position(b, 1, 50)), "position_index"),
        (lambda a, b: (_position(a, 0, True), _position(b, 1, 50)), "calculated_percent"),
        (lambda a, b: (_position(a, 0, 100), _position(_source(), 1, 0)), "unknown"),
    ],
)
def test_invalid_initial_list_is_rejected(positions, expected):
    first, second = _source(), _source()
    with pytest.raises(DraftWorkspaceError, match=expected):
        _validate_initial_positions(positions(first, second), [first, second])


def test_removed_nonzero_is_rejected():
    active, removed = _source(), _source(status="REMOVED")
    with pytest.raises(DraftWorkspaceError, match="removed.*zero"):
        _validate_initial_positions((_position(active, 0, 50), _position(removed, 1, 50)), [active, removed])


def test_saved_pin_conflict_is_rejected():
    first, second = _source(pin=25), _source()
    with pytest.raises(DraftWorkspaceError, match="saved PIN"):
        _validate_initial_positions((_position(first, 0, 30), _position(second, 1, 70)), [first, second])


def test_non_position_input_is_rejected():
    with pytest.raises(DraftWorkspaceError, match="InitialOfferPosition"):
        _validate_initial_positions(({"id": uuid4()},), [])  # type: ignore[arg-type]


def test_non_uuid_source_id_is_rejected():
    row = _source()
    with pytest.raises(DraftWorkspaceError, match="UUID"):
        _validate_initial_positions((InitialOfferPosition("bad", 0, 100),), [row])  # type: ignore[arg-type]


def test_unknown_source_status_is_rejected():
    row = _source(status="INVALID")
    with pytest.raises(DraftWorkspaceError, match="status"):
        _validate_initial_positions((_position(row, 0, 100),), [row])


def test_reject_invalid_read_id_before_query():
    with pytest.raises(DraftWorkspaceError, match="draft_id"):
        read_draft_workspace(None, "wrong")  # type: ignore[arg-type]


@pytest.fixture
def db_session():
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    yield session
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def _stream(db_session: Session) -> Stream:
    campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Workspace tests")
    row = Stream(
        keitaro_stream_id=_remote_id(),
        name="Offer stream",
        stream_type="default",
        schema_type="offers",
    )
    campaign.streams.append(row)
    db_session.add(campaign)
    db_session.flush()
    return row


def _snapshot(db_session: Session, stream: Stream) -> StreamSnapshot:
    row = StreamSnapshot.capture(
        stream_id=stream.id,
        normalized_payload={"offers": [], "nonce": _remote_id()},
    )
    db_session.add(row)
    db_session.flush()
    return row


def _saved_offer(
    db_session: Session, stream: Stream, *, status: str = "ACTIVE", pin: int | None = None,
    offer_id: int | None = None, remote_share: str = "33.7",
) -> StreamOffer:
    row = StreamOffer(
        stream_id=stream.id, keitaro_offer_id=offer_id or _remote_id(),
        local_status=status, pinned_percent=pin, remote_share=Decimal(remote_share),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _open(
    db_session: Session,
    stream: Stream,
    snap: StreamSnapshot,
    sources: tuple[tuple[StreamOffer, int], ...],
    *, created_by_id: int = 1,
):
    return open_or_create_draft_workspace(
        db_session,
        stream_id=stream.id,
        base_snapshot_id=snap.id,
        created_by_id=created_by_id,
        initial_positions=tuple(_position(source, idx, percent) for idx, (source, percent) in enumerate(sources)),
    )


def test_creates_draft_with_unequal_remote_percentages_without_rebalancing(db_session: Session):
    stream = _stream(db_session)
    snap = _snapshot(db_session, stream)
    first = _saved_offer(db_session, stream, remote_share="10.24")
    second = _saved_offer(db_session, stream, remote_share="89.76")

    view = _open(db_session, stream, snap, ((first, 10), (second, 90)), created_by_id=7)
    assert isinstance(view.draft_id, UUID)
    assert view.stream_id == stream.id
    assert view.base_snapshot_id == snap.id
    assert view.latest_remote_snapshot_id is None
    assert view.status == "SYNCED"
    assert view.created_by_id == 7
    assert view.revision == 0
    assert [offer.calculated_percent for offer in view.positions] == [10, 90]
    assert [offer.source_position_id for offer in view.positions] == [first.id, second.id]
    assert [offer.position_index for offer in view.positions] == [0, 1]
    assert db_session.get(StreamOffer, first.id).remote_share == Decimal("10.24")
    assert db_session.get(StreamOffer, second.id).remote_share == Decimal("89.76")
    assert len(db_session.scalars(select(DraftOffer).where(DraftOffer.draft_id == view.draft_id)).all()) == 2
    assert read_draft_workspace(db_session, view.draft_id) == view


def test_archived_position_is_copied_not_deleted_and_pin_is_preserved(db_session: Session):
    stream = _stream(db_session)
    snap = _snapshot(db_session, stream)
    active = _saved_offer(db_session, stream)
    archived = _saved_offer(db_session, stream, status="REMOVED", pin=40)
    view = _open(db_session, stream, snap, ((active, 100), (archived, 0)))
    assert [(item.status, item.calculated_percent) for item in view.positions] == [
        ("ACTIVE", 100), ("REMOVED", 0),
    ]
    assert view.positions[1].pinned_percent == 40
    assert db_session.get(StreamOffer, archived.id).local_status == "REMOVED"


def test_duplicate_global_offer_id_keeps_distinct_positions(db_session: Session):
    stream = _stream(db_session)
    snap = _snapshot(db_session, stream)
    shared = _remote_id()
    a = _saved_offer(db_session, stream, offer_id=shared)
    b = _saved_offer(db_session, stream, offer_id=shared)
    view = _open(db_session, stream, snap, ((a, 50), (b, 50)))
    assert view.positions[0].keitaro_offer_id == view.positions[1].keitaro_offer_id == shared
    assert view.positions[0].position_id != view.positions[1].position_id


def test_second_open_returns_existing_dirty_draft_and_keeps_local_edits(db_session: Session):
    stream = _stream(db_session)
    snap = _snapshot(db_session, stream)
    source = _saved_offer(db_session, stream)
    first = _open(db_session, stream, snap, ((source, 100),), created_by_id=1)
    draft = db_session.get(StreamDraft, first.draft_id)
    saved = db_session.get(DraftOffer, first.positions[0].position_id)
    draft.status, draft.revision = DRAFT_DIRTY, 7
    saved.pinned_percent = 100
    db_session.flush()

    # A second editor, even with outdated inputs, must NEVER overwrite the draft.
    reopened = open_or_create_draft_workspace(
        db_session, stream_id=stream.id, base_snapshot_id=uuid4(),
        created_by_id=2, initial_positions=(),
    )
    assert reopened.draft_id == first.draft_id
    assert reopened.status == DRAFT_DIRTY
    assert reopened.revision == 7
    assert reopened.created_by_id == 1
    assert reopened.positions[0].pinned_percent == 100
    assert len(db_session.scalars(select(StreamDraft).where(StreamDraft.stream_id == stream.id)).all()) == 1


@pytest.mark.parametrize("status", [DRAFT_CONFLICT, DRAFT_PUBLISHING, DRAFT_VERIFYING, DRAFT_UNCERTAIN, DRAFT_FAILED])
def test_unresolved_draft_is_reopened_never_replaced(db_session: Session, status: str):
    stream = _stream(db_session)
    snap = _snapshot(db_session, stream)
    view = _open(db_session, stream, snap, ())
    db_session.get(StreamDraft, view.draft_id).status = status
    db_session.flush()
    reopened = _open(db_session, stream, snap, ())
    assert reopened.draft_id == view.draft_id
    assert reopened.status == status


def test_after_published_draft_a_new_workspace_can_be_created(db_session: Session):
    stream = _stream(db_session)
    snap = _snapshot(db_session, stream)
    first = _open(db_session, stream, snap, ())
    db_session.get(StreamDraft, first.draft_id).status = DRAFT_PUBLISHED
    db_session.flush()
    second = _open(db_session, stream, snap, ())
    assert second.draft_id != first.draft_id
    assert len(db_session.scalars(select(StreamDraft).where(StreamDraft.stream_id == stream.id)).all()) == 2


def test_empty_offer_stream_can_open_from_snapshot_without_inventing_offers(db_session: Session):
    stream = _stream(db_session)
    snap = _snapshot(db_session, stream)
    view = _open(db_session, stream, snap, ())
    assert view.positions == ()


def test_non_offer_stream_is_not_editable_as_offer_stream(db_session: Session):
    stream = _stream(db_session)
    stream.schema_type = "redirect"
    db_session.flush()
    snap = _snapshot(db_session, stream)
    with pytest.raises(DraftWorkspaceError, match="not an offers-based stream"):
        _open(db_session, stream, snap, ())
    assert db_session.scalars(select(StreamDraft).where(StreamDraft.stream_id == stream.id)).all() == []


def test_open_requires_known_stream(db_session: Session):
    with pytest.raises(DraftWorkspaceNotFound, match="stream not found"):
        open_or_create_draft_workspace(
            db_session, stream_id=uuid4(), base_snapshot_id=uuid4(),
            created_by_id=1, initial_positions=(),
        )


def test_open_requires_captured_snapshot(db_session: Session):
    stream = _stream(db_session)
    with pytest.raises(DraftWorkspaceError, match="captured snapshot"):
        open_or_create_draft_workspace(
            db_session, stream_id=stream.id, base_snapshot_id=uuid4(),
            created_by_id=1, initial_positions=(),
        )


def test_old_snapshot_is_rejected_for_new_draft(db_session: Session):
    stream = _stream(db_session)
    old = _snapshot(db_session, stream)
    _snapshot(db_session, stream)
    with pytest.raises(DraftWorkspaceError, match="not the latest"):
        _open(db_session, stream, old, ())
    assert db_session.scalars(select(StreamDraft).where(StreamDraft.stream_id == stream.id)).all() == []


def test_other_stream_snapshot_is_rejected(db_session: Session):
    stream, other = _stream(db_session), _stream(db_session)
    _snapshot(db_session, stream)
    wrong = _snapshot(db_session, other)
    with pytest.raises(DraftWorkspaceError, match="not the latest"):
        _open(db_session, stream, wrong, ())


def test_missing_tracked_source_is_rejected_without_creating_draft(db_session: Session):
    stream = _stream(db_session)
    snap = _snapshot(db_session, stream)
    _saved_offer(db_session, stream)
    with pytest.raises(DraftWorkspaceError, match="include every"):
        _open(db_session, stream, snap, ())
    assert db_session.scalars(select(StreamDraft).where(StreamDraft.stream_id == stream.id)).all() == []


def test_percent_sum_error_leaves_no_draft(db_session: Session):
    stream = _stream(db_session)
    snap = _snapshot(db_session, stream)
    source = _saved_offer(db_session, stream)
    with pytest.raises(DraftWorkspaceError, match="sum to 100"):
        _open(db_session, stream, snap, ((source, 75),))
    assert db_session.scalars(select(StreamDraft).where(StreamDraft.stream_id == stream.id)).all() == []


@pytest.mark.parametrize("actor", [0, -1, True, 2.4])
def test_reject_invalid_creator_before_query(db_session: Session, actor):
    with pytest.raises(DraftWorkspaceError, match="created_by_id"):
        open_or_create_draft_workspace(
            db_session, stream_id=uuid4(), base_snapshot_id=uuid4(),
            created_by_id=actor, initial_positions=(),
        )


def test_read_unknown_draft_fails_without_writes(db_session: Session):
    with pytest.raises(DraftWorkspaceNotFound, match="draft not found"):
        read_draft_workspace(db_session, uuid4())
