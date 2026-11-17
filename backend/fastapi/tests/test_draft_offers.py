"""A-02/A-04 unpublished positions; database tests need Alembic 0006."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import Base
from app.models import Campaign, DraftOffer, Stream, StreamDraft, StreamOffer, StreamSnapshot
from app.models.draft_offer import DRAFT_OFFER_ACTIVE, DRAFT_OFFER_REMOVED


def _remote_id() -> int:
    return (uuid4().int % 9_000_000_000_000) + 1


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
    campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Draft offers")
    stream = Stream(
        keitaro_stream_id=_remote_id(), name="Offers",
        stream_type="default", schema_type="offers",
    )
    campaign.streams.append(stream)
    db_session.add(campaign)
    db_session.flush()
    return stream


def _draft(db_session: Session, stream: Stream, *, status: str = "SYNCED") -> StreamDraft:
    snapshot = StreamSnapshot.capture(
        stream_id=stream.id, normalized_payload={"offers": [], "nonce": _remote_id()}
    )
    db_session.add(snapshot)
    db_session.flush()
    draft = StreamDraft(
        stream_id=stream.id, base_snapshot_id=snapshot.id,
        created_by_id=1, status=status,
    )
    db_session.add(draft)
    db_session.flush()
    return draft


def _offer(draft: StreamDraft, index: int, **values) -> DraftOffer:
    return DraftOffer(
        draft_id=draft.id, stream_id=draft.stream_id,
        position_index=index, keitaro_offer_id=values.pop("keitaro_offer_id", _remote_id()),
        **values,
    )


def _source(db_session: Session, stream: Stream, *, offer_id: int | None = None) -> StreamOffer:
    row = StreamOffer(
        stream_id=stream.id, keitaro_offer_id=offer_id or _remote_id(),
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_metadata_is_separate_from_django():
    assert "kt_draft_offers" in Base.metadata.tables
    assert all(table.startswith("kt_") for table in Base.metadata.tables)
    assert DRAFT_OFFER_ACTIVE == "ACTIVE"
    assert DRAFT_OFFER_REMOVED == "REMOVED"
    constraints = {fk.name: fk for fk in DraftOffer.__table__.foreign_key_constraints}
    assert set(constraints) == {
        "fk_kt_draft_offers_draft_stream",
        "fk_kt_draft_offers_source_position_identity",
    }
    assert all(fk.ondelete == "RESTRICT" for fk in constraints.values())


def test_postgresql_composite_references_have_unique_targets():
    draft_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in StreamDraft.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    source_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in StreamOffer.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("id", "stream_id") in draft_uniques
    assert ("id", "stream_id", "keitaro_offer_id") in source_uniques
    assert all(column.name != "django_user_id" for column in DraftOffer.__table__.columns)


def test_new_positions_allow_duplicate_global_offers(db_session: Session):
    stream = _stream(db_session)
    draft = _draft(db_session, stream)
    offer_id = _remote_id()
    first = _offer(draft, 0, keitaro_offer_id=offer_id, calculated_percent=50)
    second = _offer(draft, 1, keitaro_offer_id=offer_id, calculated_percent=50)
    db_session.add_all([first, second])
    db_session.flush()
    assert isinstance(first.id, UUID)
    assert first.id != second.id
    assert first.source_position_id is None
    assert second.source_position_id is None
    assert first.draft.id == draft.id
    assert [row.id for row in db_session.scalars(
        select(DraftOffer).where(DraftOffer.draft_id == draft.id).order_by(DraftOffer.position_index)
    )] == [first.id, second.id]


def test_remote_position_can_be_copied_without_mutating_it(db_session: Session):
    stream = _stream(db_session)
    source = _source(db_session, stream)
    draft = _draft(db_session, stream)
    row = _offer(
        draft, 0, source_position_id=source.id,
        keitaro_offer_id=source.keitaro_offer_id,
        calculated_percent=100, pinned_percent=100,
    )
    db_session.add(row)
    db_session.flush()
    row.status = DRAFT_OFFER_REMOVED
    row.calculated_percent = 0
    db_session.flush()
    db_session.expire(source)
    assert source.local_status == "ACTIVE"
    assert source.keitaro_offer_id == row.keitaro_offer_id
    assert row.status == DRAFT_OFFER_REMOVED
    assert row.calculated_percent == 0
    assert row.pinned_percent == 100  # preserve the pin for future BRING BACK handling


def test_raw_insert_defaults_to_active_and_zero(db_session: Session):
    draft = _draft(db_session, _stream(db_session))
    row_id = uuid4()
    db_session.execute(
        text(
            "INSERT INTO kt_draft_offers "
            "(id, draft_id, stream_id, keitaro_offer_id, position_index) "
            "VALUES (:id, :draft_id, :stream_id, :offer_id, 0)"
        ),
        {"id": row_id, "draft_id": draft.id, "stream_id": draft.stream_id, "offer_id": _remote_id()},
    )
    row = db_session.get(DraftOffer, row_id)
    assert row is not None
    assert row.status == DRAFT_OFFER_ACTIVE
    assert row.calculated_percent == 0
    assert row.pinned_percent is None


def test_rejected_orphan_draft(db_session: Session):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(DraftOffer(
                draft_id=uuid4(), stream_id=uuid4(), keitaro_offer_id=_remote_id(),
                position_index=0,
            ))
            db_session.flush()


def test_reject_draft_offer_with_another_stream(db_session: Session):
    draft = _draft(db_session, _stream(db_session))
    other = _stream(db_session)
    row = _offer(draft, 0)
    row.stream_id = other.id
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(row)
            db_session.flush()


def test_reject_source_offer_from_other_stream(db_session: Session):
    draft = _draft(db_session, _stream(db_session))
    other_source = _source(db_session, _stream(db_session))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_offer(
                draft, 0, source_position_id=other_source.id,
                keitaro_offer_id=other_source.keitaro_offer_id,
            ))
            db_session.flush()


def test_reject_source_offer_id_mismatch(db_session: Session):
    stream = _stream(db_session)
    source = _source(db_session, stream)
    draft = _draft(db_session, stream)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_offer(
                draft, 0, source_position_id=source.id,
                keitaro_offer_id=source.keitaro_offer_id + 1,
            ))
            db_session.flush()


def test_reject_nonexistent_source_position(db_session: Session):
    draft = _draft(db_session, _stream(db_session))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_offer(draft, 0, source_position_id=uuid4()))
            db_session.flush()


def test_reject_duplicate_order_in_same_draft(db_session: Session):
    draft = _draft(db_session, _stream(db_session))
    db_session.add(_offer(draft, 0))
    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_offer(draft, 0))
            db_session.flush()


def test_reject_duplicate_source_in_same_draft(db_session: Session):
    stream = _stream(db_session)
    source = _source(db_session, stream)
    draft = _draft(db_session, stream)
    db_session.add(_offer(
        draft, 0, source_position_id=source.id,
        keitaro_offer_id=source.keitaro_offer_id,
    ))
    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_offer(
                draft, 1, source_position_id=source.id,
                keitaro_offer_id=source.keitaro_offer_id,
            ))
            db_session.flush()


def test_same_source_can_appear_in_different_draft_generations(db_session: Session):
    stream = _stream(db_session)
    source = _source(db_session, stream)
    old = _draft(db_session, stream, status="PUBLISHED")
    current = _draft(db_session, stream)
    db_session.add_all([
        _offer(old, 0, source_position_id=source.id, keitaro_offer_id=source.keitaro_offer_id),
        _offer(current, 0, source_position_id=source.id, keitaro_offer_id=source.keitaro_offer_id),
    ])
    db_session.flush()


@pytest.mark.parametrize("bad_id", [0, -2])
def test_reject_nonpositive_offer_id(db_session: Session, bad_id: int):
    draft = _draft(db_session, _stream(db_session))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_offer(draft, 0, keitaro_offer_id=bad_id))
            db_session.flush()


@pytest.mark.parametrize("bad_order", [-1, -5])
def test_reject_negative_position(db_session: Session, bad_order: int):
    draft = _draft(db_session, _stream(db_session))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_offer(draft, bad_order))
            db_session.flush()


@pytest.mark.parametrize("bad_status", ["DELETED", "", "ARCHIVED"])
def test_reject_invalid_status(db_session: Session, bad_status: str):
    draft = _draft(db_session, _stream(db_session))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_offer(draft, 0, status=bad_status))
            db_session.flush()


@pytest.mark.parametrize("bad_percent", [-1, 101])
def test_reject_invalid_calculated_percent(db_session: Session, bad_percent: int):
    draft = _draft(db_session, _stream(db_session))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_offer(draft, 0, calculated_percent=bad_percent))
            db_session.flush()


@pytest.mark.parametrize("bad_pin", [-1, 101])
def test_reject_invalid_pin(db_session: Session, bad_pin: int):
    draft = _draft(db_session, _stream(db_session))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_offer(draft, 0, pinned_percent=bad_pin))
            db_session.flush()


def test_reject_nonzero_removed_offer(db_session: Session):
    draft = _draft(db_session, _stream(db_session))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_offer(draft, 0, status=DRAFT_OFFER_REMOVED, calculated_percent=25))
            db_session.flush()


@pytest.mark.parametrize("valid_pin", [0, 100])
def test_accept_valid_pin_boundaries(db_session: Session, valid_pin: int):
    draft = _draft(db_session, _stream(db_session))
    db_session.add(_offer(draft, 0, pinned_percent=valid_pin, calculated_percent=valid_pin))
    db_session.flush()


def test_prevent_draft_deletion_with_positions(db_session: Session):
    draft = _draft(db_session, _stream(db_session))
    db_session.add(_offer(draft, 0))
    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(delete(StreamDraft).where(StreamDraft.id == draft.id))


def test_prevent_source_deletion_with_draft_reference(db_session: Session):
    stream = _stream(db_session)
    source = _source(db_session, stream)
    draft = _draft(db_session, stream)
    db_session.add(_offer(
        draft, 0, source_position_id=source.id, keitaro_offer_id=source.keitaro_offer_id
    ))
    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(delete(StreamOffer).where(StreamOffer.id == source.id))
