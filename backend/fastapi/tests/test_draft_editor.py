"""A-04 ADD/REMOVE: local-only transactional edits and optimistic revision checks.

Pure input validation runs without PostgreSQL. The database cases require
PostgreSQL with Alembic revision 0006_draft_offers (unchanged by this patch).
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Campaign, Stream, StreamDraft, StreamOffer, StreamSnapshot
from app.services.draft_editor import (
    DraftEditBlocked,
    DraftEditError,
    DraftEditNotFound,
    DraftRevisionConflict,
    add_offer_to_draft,
    remove_offer_from_draft,
)
from app.services.draft_workspace import (
    InitialOfferPosition,
    open_or_create_draft_workspace,
    read_draft_workspace,
)


def _remote_id() -> int:
    return (uuid4().int % 9_000_000_000_000) + 1


@pytest.mark.parametrize("bad_id", [None, "bad", 10, True])
def test_add_rejects_invalid_draft_id_without_database_access(bad_id):
    with pytest.raises(DraftEditError, match="draft_id"):
        add_offer_to_draft(None, draft_id=bad_id, expected_revision=0, keitaro_offer_id=1)


@pytest.mark.parametrize("bad_revision", [-1, 1.2, True, "1", 2_147_483_647])
def test_add_rejects_invalid_revision_without_database_access(bad_revision):
    with pytest.raises(DraftEditError, match="expected_revision"):
        add_offer_to_draft(None, draft_id=uuid4(), expected_revision=bad_revision, keitaro_offer_id=1)


@pytest.mark.parametrize("bad_offer", [0, -1, True, 1.2, "123", 9_223_372_036_854_775_808])
def test_add_rejects_invalid_global_offer_id_without_database_access(bad_offer):
    with pytest.raises(DraftEditError, match="keitaro_offer_id"):
        add_offer_to_draft(None, draft_id=uuid4(), expected_revision=0, keitaro_offer_id=bad_offer)


@pytest.mark.parametrize("bad_position", [None, "bad", 15, False])
def test_remove_rejects_invalid_position_id_without_database_access(bad_position):
    with pytest.raises(DraftEditError, match="position_id"):
        remove_offer_from_draft(None, draft_id=uuid4(), expected_revision=0, position_id=bad_position)


def test_remove_rejects_bad_revision_before_database_access():
    with pytest.raises(DraftEditError, match="expected_revision"):
        remove_offer_from_draft(None, draft_id=uuid4(), expected_revision=-1, position_id=uuid4())


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


def _stream(session: Session) -> Stream:
    campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Editor transaction test")
    stream = Stream(
        keitaro_stream_id=_remote_id(), name="Draft offer stream",
        stream_type="default", schema_type="offers",
    )
    campaign.streams.append(stream)
    session.add(campaign)
    session.flush()
    return stream


def _source(session: Session, stream: Stream, *, pin: int | None = None,
            offer_id: int | None = None) -> StreamOffer:
    source = StreamOffer(
        stream_id=stream.id,
        keitaro_offer_id=offer_id or _remote_id(),
        pinned_percent=pin,
        local_status="ACTIVE",
    )
    session.add(source)
    session.flush()
    return source


def _workspace(session: Session, percentages: tuple[int, ...],
               *, pins: tuple[int | None, ...] | None = None,
               ids: tuple[int, ...] | None = None):
    stream = _stream(session)
    snapshot = StreamSnapshot.capture(
        stream_id=stream.id,
        normalized_payload={"offers": [], "test": _remote_id()},
    )
    session.add(snapshot)
    session.flush()
    if pins is None:
        pins = (None,) * len(percentages)
    assert len(pins) == len(percentages)
    sources = [
        _source(session, stream, pin=pin, offer_id=ids[i] if ids else None)
        for i, pin in enumerate(pins)
    ]
    view = open_or_create_draft_workspace(
        session,
        stream_id=stream.id,
        base_snapshot_id=snapshot.id,
        created_by_id=1,
        initial_positions=tuple(
            InitialOfferPosition(src.id, i, percentage)
            for i, (src, percentage) in enumerate(zip(sources, percentages))
        ),
    )
    return stream, sources, view


def _percents(view):
    return [position.calculated_percent for position in view.positions]


def test_add_fourth_offer_rebalances_only_the_draft(db_session: Session):
    stream, sources, original = _workspace(db_session, (34, 33, 33))
    new_global_id = _remote_id()
    view = add_offer_to_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        keitaro_offer_id=new_global_id,
    )
    assert view.status == "DIRTY"
    assert view.revision == 1
    assert _percents(view) == [25, 25, 25, 25]
    assert view.positions[-1].position_index == 3
    assert isinstance(view.positions[-1].position_id, UUID)
    assert view.positions[-1].source_position_id is None
    assert view.positions[-1].keitaro_offer_id == new_global_id
    assert view.base_snapshot_id == original.base_snapshot_id
    assert read_draft_workspace(db_session, view.draft_id) == view
    assert [db_session.get(StreamOffer, source.id).local_status for source in sources] == ["ACTIVE"] * 3
    assert len(db_session.scalars(select(StreamOffer).where(StreamOffer.stream_id == stream.id)).all()) == 3


def test_add_same_global_offer_creates_distinct_local_position(db_session: Session):
    _stream_row, sources, original = _workspace(db_session, (100,))
    view = add_offer_to_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        keitaro_offer_id=sources[0].keitaro_offer_id,
    )
    assert _percents(view) == [50, 50]
    assert view.positions[0].keitaro_offer_id == view.positions[1].keitaro_offer_id
    assert view.positions[0].position_id != view.positions[1].position_id
    assert view.positions[1].source_position_id is None


def test_add_respects_25_percent_pin_and_remainder_order(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, (25, 75), pins=(25, None))
    view = add_offer_to_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        keitaro_offer_id=_remote_id(),
    )
    assert _percents(view) == [25, 38, 37]
    assert view.positions[0].pinned_percent == 25


def test_add_to_empty_offer_draft_gives_new_position_100(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, ())
    view = add_offer_to_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        keitaro_offer_id=_remote_id(),
    )
    assert _percents(view) == [100]
    assert view.revision == 1


def test_add_after_all_pinned_100_gives_new_free_position_zero(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, (100,), pins=(100,))
    view = add_offer_to_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        keitaro_offer_id=_remote_id(),
    )
    assert _percents(view) == [100, 0]


def test_remove_keeps_gray_archived_position_and_zero_percent(db_session: Session):
    stream, sources, original = _workspace(db_session, (34, 33, 33))
    target = original.positions[1]
    view = remove_offer_from_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        position_id=target.position_id,
    )
    assert view.status == "DIRTY"
    assert view.revision == 1
    assert _percents(view) == [50, 0, 50]
    assert view.positions[1].position_id == target.position_id
    assert view.positions[1].status == "REMOVED"
    assert view.positions[1].source_position_id == sources[1].id
    assert db_session.get(StreamOffer, sources[1].id).local_status == "ACTIVE"
    assert len(db_session.scalars(select(StreamOffer).where(StreamOffer.stream_id == stream.id)).all()) == 3


def test_remove_pinned_offer_preserves_its_saved_pin_for_future_restore(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, (25, 75), pins=(25, None))
    view = remove_offer_from_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        position_id=original.positions[0].position_id,
    )
    assert _percents(view) == [0, 100]
    assert view.positions[0].pinned_percent == 25
    assert view.positions[0].status == "REMOVED"


def test_remove_last_active_offer_is_zeros_not_a_remote_delete(db_session: Session):
    _stream_row, sources, original = _workspace(db_session, (100,))
    view = remove_offer_from_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        position_id=original.positions[0].position_id,
    )
    assert _percents(view) == [0]
    assert view.positions[0].status == "REMOVED"
    assert db_session.get(StreamOffer, sources[0].id).local_status == "ACTIVE"


def test_remove_only_requested_local_position_when_global_id_is_duplicate(db_session: Session):
    shared = _remote_id()
    _stream_row, _sources, original = _workspace(db_session, (50, 50), ids=(shared, shared))
    view = remove_offer_from_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        position_id=original.positions[1].position_id,
    )
    assert _percents(view) == [100, 0]
    assert [p.status for p in view.positions] == ["ACTIVE", "REMOVED"]


def test_remove_unpublished_new_offer_archives_local_row_only(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, (100,))
    added = add_offer_to_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        keitaro_offer_id=_remote_id(),
    )
    archived = remove_offer_from_draft(
        db_session, draft_id=original.draft_id, expected_revision=1,
        position_id=added.positions[1].position_id,
    )
    assert archived.revision == 2
    assert _percents(archived) == [100, 0]
    assert archived.positions[1].source_position_id is None
    assert archived.positions[1].status == "REMOVED"


def test_stale_revision_rejected_without_creating_an_offer(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, (100,))
    updated = add_offer_to_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        keitaro_offer_id=_remote_id(),
    )
    with pytest.raises(DraftRevisionConflict, match="stale"):
        add_offer_to_draft(
            db_session, draft_id=original.draft_id, expected_revision=0,
            keitaro_offer_id=_remote_id(),
        )
    assert read_draft_workspace(db_session, original.draft_id) == updated


def test_fresh_revision_allows_next_edit_and_uses_consecutive_order(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, (100,))
    first = add_offer_to_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        keitaro_offer_id=_remote_id(),
    )
    second = add_offer_to_draft(
        db_session, draft_id=original.draft_id, expected_revision=first.revision,
        keitaro_offer_id=_remote_id(),
    )
    assert second.revision == 2
    assert _percents(second) == [34, 33, 33]
    assert [p.position_index for p in second.positions] == [0, 1, 2]


def test_second_remove_rejected_without_revision_increment(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, (100,))
    view = remove_offer_from_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        position_id=original.positions[0].position_id,
    )
    with pytest.raises(DraftEditError, match="already removed"):
        remove_offer_from_draft(
            db_session, draft_id=original.draft_id, expected_revision=1,
            position_id=original.positions[0].position_id,
        )
    assert read_draft_workspace(db_session, original.draft_id) == view


def test_position_of_different_draft_is_not_removable(db_session: Session):
    _stream_one, _sources_one, first = _workspace(db_session, (100,))
    _stream_two, _sources_two, second = _workspace(db_session, (100,))
    with pytest.raises(DraftEditNotFound, match="position"):
        remove_offer_from_draft(
            db_session, draft_id=first.draft_id, expected_revision=0,
            position_id=second.positions[0].position_id,
        )
    assert read_draft_workspace(db_session, first.draft_id) == first


def test_missing_position_rejected_without_changes(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, (100,))
    with pytest.raises(DraftEditNotFound, match="position"):
        remove_offer_from_draft(
            db_session, draft_id=original.draft_id, expected_revision=0,
            position_id=uuid4(),
        )
    assert read_draft_workspace(db_session, original.draft_id) == original


def test_missing_draft_rejected_for_both_actions(db_session: Session):
    with pytest.raises(DraftEditNotFound, match="draft not found"):
        add_offer_to_draft(db_session, draft_id=uuid4(), expected_revision=0, keitaro_offer_id=1)
    with pytest.raises(DraftEditNotFound, match="draft not found"):
        remove_offer_from_draft(db_session, draft_id=uuid4(), expected_revision=0, position_id=uuid4())


@pytest.mark.parametrize(
    "blocked_status",
    ["CONFLICT", "PUBLISHING", "VERIFYING", "UNCERTAIN", "FAILED", "PUBLISHED", "CANCELLED"],
)
def test_noneditable_draft_status_blocks_changes(db_session: Session, blocked_status: str):
    _stream_row, _sources, original = _workspace(db_session, (100,))
    draft = db_session.get(StreamDraft, original.draft_id)
    draft.status = blocked_status
    db_session.flush()
    with pytest.raises(DraftEditBlocked, match="does not permit"):
        add_offer_to_draft(
            db_session, draft_id=draft.id, expected_revision=0,
            keitaro_offer_id=_remote_id(),
        )
    with pytest.raises(DraftEditBlocked, match="does not permit"):
        remove_offer_from_draft(
            db_session, draft_id=draft.id, expected_revision=0,
            position_id=original.positions[0].position_id,
        )
    assert db_session.get(StreamDraft, draft.id).revision == 0


def test_cannot_remove_only_free_offer_when_another_active_pin_is_25(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, (25, 75), pins=(25, None))
    with pytest.raises(DraftEditError, match="all active positions are pinned"):
        remove_offer_from_draft(
            db_session, draft_id=original.draft_id, expected_revision=0,
            position_id=original.positions[1].position_id,
        )
    assert read_draft_workspace(db_session, original.draft_id) == original


def test_editor_can_add_to_dirty_draft_without_changing_base_snapshot(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, (100,))
    first = add_offer_to_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        keitaro_offer_id=_remote_id(),
    )
    second = add_offer_to_draft(
        db_session, draft_id=first.draft_id, expected_revision=first.revision,
        keitaro_offer_id=_remote_id(),
    )
    assert second.status == "DIRTY"
    assert second.base_snapshot_id == original.base_snapshot_id
    assert second.latest_remote_snapshot_id == original.latest_remote_snapshot_id
    assert len(db_session.scalars(select(StreamSnapshot).where(StreamSnapshot.stream_id == second.stream_id)).all()) == 1


def test_stream_changed_to_redirect_is_not_editable(db_session: Session):
    stream, _sources, original = _workspace(db_session, (100,))
    stream.schema_type = "redirect"
    db_session.flush()
    with pytest.raises(DraftEditBlocked, match="offer-based"):
        add_offer_to_draft(
            db_session, draft_id=original.draft_id, expected_revision=0,
            keitaro_offer_id=_remote_id(),
        )
    assert read_draft_workspace(db_session, original.draft_id) == original


def test_changes_rollback_when_caller_aborts_transaction(db_session: Session):
    _stream_row, _sources, original = _workspace(db_session, (100,))
    with pytest.raises(RuntimeError, match="caller abort"):
        with db_session.begin_nested():
            updated = add_offer_to_draft(
                db_session, draft_id=original.draft_id, expected_revision=0,
                keitaro_offer_id=_remote_id(),
            )
            assert updated.revision == 1
            assert _percents(updated) == [50, 50]
            raise RuntimeError("caller abort")
    assert read_draft_workspace(db_session, original.draft_id) == original
