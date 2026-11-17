"""A-04: BRING BACK, PIN and UNPIN are local and revision-safe.

Validation tests need no database. Integration tests use PostgreSQL with
Alembic revision 0006_draft_offers, and never contact Keitaro.
"""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Campaign, DraftOffer, Stream, StreamDraft, StreamOffer, StreamSnapshot
from app.services.draft_editor import (
    DraftEditBlocked,
    DraftEditError,
    DraftEditNotFound,
    DraftRevisionConflict,
    add_offer_to_draft,
    bring_back_offer_in_draft,
    pin_offer_in_draft,
    remove_offer_from_draft,
)
from app.services.draft_workspace import (
    InitialOfferPosition,
    open_or_create_draft_workspace,
    read_draft_workspace,
)


def _remote_id() -> int:
    return (uuid4().int % 9_000_000_000_000) + 1


def _percents(view):
    return [row.calculated_percent for row in view.positions]


@pytest.mark.parametrize("bad_id", [None, "bad", 42, True])
def test_bring_back_rejects_invalid_draft_id_before_database(bad_id):
    with pytest.raises(DraftEditError, match="draft_id"):
        bring_back_offer_in_draft(None, draft_id=bad_id, expected_revision=0, position_id=uuid4())


@pytest.mark.parametrize("bad_revision", [-1, "0", True, 1.5, 2_147_483_647])
def test_bring_back_rejects_invalid_revision_before_database(bad_revision):
    with pytest.raises(DraftEditError, match="expected_revision"):
        bring_back_offer_in_draft(None, draft_id=uuid4(), expected_revision=bad_revision, position_id=uuid4())


@pytest.mark.parametrize("bad_position", [None, "bad", 19, False])
def test_bring_back_rejects_invalid_position_id_before_database(bad_position):
    with pytest.raises(DraftEditError, match="position_id"):
        bring_back_offer_in_draft(None, draft_id=uuid4(), expected_revision=0, position_id=bad_position)


@pytest.mark.parametrize("bad_pin", [-1, 101, 1.0, True, "25", 10**50])
def test_pin_rejects_invalid_value_before_database(bad_pin):
    with pytest.raises(DraftEditError, match="pinned_percent"):
        pin_offer_in_draft(None, draft_id=uuid4(), expected_revision=0, position_id=uuid4(), pinned_percent=bad_pin)


def test_pin_accepts_none_or_boundary_values_at_validation_layer():
    # Valid values reach the DB lookup, rather than being rejected by input validation.
    for value in (None, 0, 100):
        with pytest.raises(AttributeError):
            pin_offer_in_draft(None, draft_id=uuid4(), expected_revision=0, position_id=uuid4(), pinned_percent=value)


@pytest.mark.parametrize("bad_id", [None, "bad", 3])
def test_pin_rejects_invalid_draft_id_before_database(bad_id):
    with pytest.raises(DraftEditError, match="draft_id"):
        pin_offer_in_draft(None, draft_id=bad_id, expected_revision=0, position_id=uuid4(), pinned_percent=25)


@pytest.mark.parametrize("bad_revision", [-1, True, "0", 2_147_483_647])
def test_pin_rejects_invalid_revision_before_database(bad_revision):
    with pytest.raises(DraftEditError, match="expected_revision"):
        pin_offer_in_draft(None, draft_id=uuid4(), expected_revision=bad_revision, position_id=uuid4(), pinned_percent=25)


@pytest.mark.parametrize("bad_position", [None, "bad", 9, True])
def test_pin_rejects_invalid_position_id_before_database(bad_position):
    with pytest.raises(DraftEditError, match="position_id"):
        pin_offer_in_draft(None, draft_id=uuid4(), expected_revision=0, position_id=bad_position, pinned_percent=25)


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


def _workspace(
    session: Session,
    percentages: tuple[int, ...],
    *,
    pins: tuple[int | None, ...] | None = None,
    archived: tuple[bool, ...] | None = None,
    offer_ids: tuple[int, ...] | None = None,
):
    campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Controls test")
    stream = Stream(
        keitaro_stream_id=_remote_id(), name="Editor stream",
        stream_type="default", schema_type="offers",
    )
    campaign.streams.append(stream)
    session.add(campaign)
    session.flush()
    snapshot = StreamSnapshot.capture(
        stream_id=stream.id, normalized_payload={"offers": [], "nonce": _remote_id()}
    )
    session.add(snapshot)
    session.flush()
    pins = pins if pins is not None else (None,) * len(percentages)
    archived = archived if archived is not None else (False,) * len(percentages)
    assert len(pins) == len(archived) == len(percentages)
    if offer_ids is not None:
        assert len(offer_ids) == len(percentages)
    sources = []
    for index, _ in enumerate(percentages):
        source = StreamOffer(
            stream_id=stream.id,
            keitaro_offer_id=offer_ids[index] if offer_ids else _remote_id(),
            local_status="REMOVED" if archived[index] else "ACTIVE",
            pinned_percent=pins[index],
        )
        session.add(source)
        sources.append(source)
    session.flush()
    draft = open_or_create_draft_workspace(
        session,
        stream_id=stream.id,
        base_snapshot_id=snapshot.id,
        created_by_id=1,
        initial_positions=tuple(
            InitialOfferPosition(source.id, index, percentages[index])
            for index, source in enumerate(sources)
        ),
    )
    return stream, sources, draft


def test_bring_back_removed_position_recalculates_without_deleting_archive(db_session: Session):
    stream, sources, original = _workspace(db_session, (50, 0, 50), archived=(False, True, False))
    target = original.positions[1]
    restored = bring_back_offer_in_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        position_id=target.position_id,
    )
    assert restored.revision == 1
    assert restored.status == "DIRTY"
    assert _percents(restored) == [34, 33, 33]
    assert restored.positions[1].position_id == target.position_id
    assert restored.positions[1].source_position_id == sources[1].id
    assert restored.positions[1].keitaro_offer_id == target.keitaro_offer_id
    assert restored.positions[1].status == "ACTIVE"
    assert db_session.get(StreamOffer, sources[1].id).local_status == "REMOVED"
    assert len(db_session.scalars(select(StreamOffer).where(StreamOffer.stream_id == stream.id)).all()) == 3


def test_bring_back_saved_pin_reserves_exact_weight(db_session: Session):
    _stream, _sources, original = _workspace(
        db_session, (50, 0, 50), pins=(None, 25, None), archived=(False, True, False)
    )
    restored = bring_back_offer_in_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        position_id=original.positions[1].position_id,
    )
    assert _percents(restored) == [38, 25, 37]
    assert restored.positions[1].pinned_percent == 25
    assert restored.positions[1].status == "ACTIVE"


def test_remove_then_bring_back_new_unpublished_position(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (100,))
    added = add_offer_to_draft(
        db_session, draft_id=original.draft_id, expected_revision=0, keitaro_offer_id=_remote_id()
    )
    target = added.positions[1]
    removed = remove_offer_from_draft(
        db_session, draft_id=added.draft_id, expected_revision=1, position_id=target.position_id
    )
    assert _percents(removed) == [100, 0]
    restored = bring_back_offer_in_draft(
        db_session, draft_id=removed.draft_id, expected_revision=2, position_id=target.position_id
    )
    assert restored.revision == 3
    assert _percents(restored) == [50, 50]
    assert restored.positions[1].position_id == target.position_id
    assert restored.positions[1].source_position_id is None


def test_bring_back_one_of_duplicate_offer_ids_preserves_other_position(db_session: Session):
    shared = _remote_id()
    _stream, _sources, original = _workspace(
        db_session, (100, 0), archived=(False, True), offer_ids=(shared, shared)
    )
    view = bring_back_offer_in_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        position_id=original.positions[1].position_id,
    )
    assert _percents(view) == [50, 50]
    assert view.positions[0].position_id != view.positions[1].position_id
    assert view.positions[0].keitaro_offer_id == view.positions[1].keitaro_offer_id == shared


def test_bring_back_rejects_already_active_without_increment(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (100,))
    with pytest.raises(DraftEditError, match="already active"):
        bring_back_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                                  position_id=original.positions[0].position_id)
    assert read_draft_workspace(db_session, original.draft_id) == original


def test_bring_back_rejects_missing_position(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (100,))
    with pytest.raises(DraftEditNotFound, match="position"):
        bring_back_offer_in_draft(db_session, draft_id=original.draft_id,
                                  expected_revision=0, position_id=uuid4())
    assert read_draft_workspace(db_session, original.draft_id) == original


def test_bring_back_cannot_touch_another_draft(db_session: Session):
    _stream1, _sources1, first = _workspace(db_session, (100,))
    _stream2, _sources2, second = _workspace(db_session, (0,), archived=(True,))
    with pytest.raises(DraftEditNotFound, match="position"):
        bring_back_offer_in_draft(db_session, draft_id=first.draft_id, expected_revision=0,
                                  position_id=second.positions[0].position_id)
    assert read_draft_workspace(db_session, first.draft_id) == first


def test_bring_back_with_saved_pin_fails_if_no_free_slot(db_session: Session):
    _stream, _sources, original = _workspace(
        db_session, (100, 0), pins=(100, 25), archived=(False, True)
    )
    with pytest.raises(DraftEditError, match="exceeds 100"):
        bring_back_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                                  position_id=original.positions[1].position_id)
    assert read_draft_workspace(db_session, original.draft_id) == original


def test_bring_back_after_pin_100_allows_free_position_at_zero(db_session: Session):
    _stream, _sources, original = _workspace(
        db_session, (100, 0), pins=(100, None), archived=(False, True)
    )
    restored = bring_back_offer_in_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        position_id=original.positions[1].position_id,
    )
    assert _percents(restored) == [100, 0]
    assert restored.positions[1].status == "ACTIVE"


def test_pin_25_preserves_it_while_other_positions_rebalance(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (34, 33, 33))
    pinned = pin_offer_in_draft(
        db_session, draft_id=original.draft_id, expected_revision=0,
        position_id=original.positions[0].position_id, pinned_percent=25,
    )
    assert _percents(pinned) == [25, 38, 37]
    assert pinned.positions[0].pinned_percent == 25
    assert pinned.revision == 1
    assert pinned.status == "DIRTY"
    added = add_offer_to_draft(db_session, draft_id=pinned.draft_id,
                               expected_revision=1, keitaro_offer_id=_remote_id())
    assert _percents(added) == [25, 25, 25, 25]
    assert added.positions[0].pinned_percent == 25


def test_pin_0_is_a_real_pin(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (50, 50))
    view = pin_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                              position_id=original.positions[0].position_id, pinned_percent=0)
    assert _percents(view) == [0, 100]
    assert view.positions[0].pinned_percent == 0


def test_pin_100_leaves_zero_for_other_active_positions(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (50, 50))
    view = pin_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                              position_id=original.positions[1].position_id, pinned_percent=100)
    assert _percents(view) == [0, 100]
    assert view.positions[1].pinned_percent == 100


def test_unpin_rebalances_all_active_positions(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (25, 75), pins=(25, None))
    view = pin_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                              position_id=original.positions[0].position_id, pinned_percent=None)
    assert _percents(view) == [50, 50]
    assert view.positions[0].pinned_percent is None
    assert view.revision == 1


def test_pin_change_from_25_to_40_redistributes_remaining(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (25, 38, 37), pins=(25, None, None))
    view = pin_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                              position_id=original.positions[0].position_id, pinned_percent=40)
    assert _percents(view) == [40, 30, 30]


def test_pin_twice_same_value_is_rejected_without_dirtying(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (25, 75), pins=(25, None))
    with pytest.raises(DraftEditError, match="already has"):
        pin_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                           position_id=original.positions[0].position_id, pinned_percent=25)
    assert read_draft_workspace(db_session, original.draft_id) == original


def test_unpin_without_existing_pin_is_rejected_without_dirtying(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (100,))
    with pytest.raises(DraftEditError, match="already has"):
        pin_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                           position_id=original.positions[0].position_id, pinned_percent=None)
    assert read_draft_workspace(db_session, original.draft_id) == original


def test_pin_fails_if_combined_active_pins_exceed_100(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (60, 40), pins=(60, None))
    with pytest.raises(DraftEditError, match="exceeds 100"):
        pin_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                           position_id=original.positions[1].position_id, pinned_percent=41)
    assert read_draft_workspace(db_session, original.draft_id) == original


def test_pin_fails_if_every_active_position_pinned_below_100(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (50, 50), pins=(50, None))
    with pytest.raises(DraftEditError, match="all active positions are pinned"):
        pin_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                           position_id=original.positions[1].position_id, pinned_percent=40)
    assert read_draft_workspace(db_session, original.draft_id) == original


def test_pin_cannot_modify_archived_position(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (100, 0), archived=(False, True))
    with pytest.raises(DraftEditBlocked, match="only active"):
        pin_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                           position_id=original.positions[1].position_id, pinned_percent=25)
    assert read_draft_workspace(db_session, original.draft_id) == original


def test_pin_cannot_touch_position_from_another_draft(db_session: Session):
    _stream1, _sources1, first = _workspace(db_session, (100,))
    _stream2, _sources2, second = _workspace(db_session, (100,))
    with pytest.raises(DraftEditNotFound, match="position"):
        pin_offer_in_draft(db_session, draft_id=first.draft_id, expected_revision=0,
                           position_id=second.positions[0].position_id, pinned_percent=25)
    assert read_draft_workspace(db_session, first.draft_id) == first


def test_bring_back_and_pin_stale_revision_rejected(db_session: Session):
    _stream, _sources, original = _workspace(db_session, (100, 0), archived=(False, True))
    current = bring_back_offer_in_draft(db_session, draft_id=original.draft_id,
                                        expected_revision=0, position_id=original.positions[1].position_id)
    with pytest.raises(DraftRevisionConflict, match="stale"):
        bring_back_offer_in_draft(db_session, draft_id=original.draft_id,
                                  expected_revision=0, position_id=original.positions[1].position_id)
    with pytest.raises(DraftRevisionConflict, match="stale"):
        pin_offer_in_draft(db_session, draft_id=original.draft_id,
                           expected_revision=0, position_id=original.positions[0].position_id, pinned_percent=25)
    assert read_draft_workspace(db_session, original.draft_id) == current


@pytest.mark.parametrize("status", [
    "CONFLICT", "PUBLISHING", "VERIFYING", "UNCERTAIN", "FAILED", "PUBLISHED", "CANCELLED",
])
def test_bring_back_and_pin_blocked_in_noneditable_status(db_session: Session, status: str):
    _stream, _sources, original = _workspace(db_session, (100, 0), archived=(False, True))
    draft = db_session.get(StreamDraft, original.draft_id)
    draft.status = status
    db_session.flush()
    with pytest.raises(DraftEditBlocked, match="does not permit"):
        bring_back_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                                  position_id=original.positions[1].position_id)
    with pytest.raises(DraftEditBlocked, match="does not permit"):
        pin_offer_in_draft(db_session, draft_id=original.draft_id, expected_revision=0,
                           position_id=original.positions[0].position_id, pinned_percent=25)
    assert draft.revision == 0


def test_bring_back_and_pin_do_not_commit_if_transaction_rolls_back(db_session: Session):
    _stream, sources, original = _workspace(db_session, (100, 0), archived=(False, True))
    with pytest.raises(RuntimeError, match="rollback"):
        with db_session.begin_nested():
            restored = bring_back_offer_in_draft(
                db_session, draft_id=original.draft_id, expected_revision=0,
                position_id=original.positions[1].position_id,
            )
            pinned = pin_offer_in_draft(
                db_session, draft_id=original.draft_id, expected_revision=1,
                position_id=restored.positions[0].position_id, pinned_percent=25,
            )
            assert pinned.revision == 2
            assert _percents(pinned) == [25, 75]
            raise RuntimeError("rollback")
    assert read_draft_workspace(db_session, original.draft_id) == original
    assert db_session.get(StreamOffer, sources[1].id).local_status == "REMOVED"
    assert db_session.scalar(select(DraftOffer).where(DraftOffer.id == original.positions[1].position_id)).status == "REMOVED"
