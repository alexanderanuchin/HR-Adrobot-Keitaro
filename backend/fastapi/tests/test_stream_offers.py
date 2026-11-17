"""A-04 offer-position tests; DB cases require Alembic at 0004_stream_offers."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import Base
from app.models import Campaign, Stream, StreamOffer
from app.models.stream_offer import LOCAL_OFFER_ACTIVE, LOCAL_OFFER_REMOVED


def _remote_id() -> int:
    return (uuid4().int % 9_000_000_000_000) + 1


@pytest.fixture
def db_session():
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(
                    bind=connection, join_transaction_mode="create_savepoint"
                ) as session:
                    yield session
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.fixture
def stream(db_session: Session) -> Stream:
    campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Offer positions")
    result = Stream(
        keitaro_stream_id=_remote_id(),
        name="Offers stream",
        stream_type="default",
        schema_type="offers",
    )
    campaign.streams.append(result)
    db_session.add(campaign)
    db_session.flush()
    return result


def test_offer_position_metadata_respects_orm_ownership():
    assert "kt_stream_offers" in Base.metadata.tables
    assert all(name.startswith("kt_") for name in Base.metadata.tables)
    fk = next(iter(StreamOffer.__table__.c.stream_id.foreign_keys))
    assert fk.target_fullname == "kt_streams.id"
    assert fk.ondelete == "RESTRICT"


def test_no_unique_constraint_for_same_offer_in_same_stream():
    unique_constraints = [
        tuple(column.name for column in constraint.columns)
        for constraint in StreamOffer.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    ]
    assert ("stream_id", "keitaro_offer_id") not in unique_constraints


def test_same_keitaro_offer_can_appear_in_two_local_positions(
    db_session: Session, stream: Stream
):
    offer_id = _remote_id()
    first = StreamOffer(
        stream_id=stream.id,
        keitaro_offer_id=offer_id,
        keitaro_relation_id=_remote_id(),
        remote_share=Decimal("30.5000"),
    )
    second = StreamOffer(
        stream_id=stream.id,
        keitaro_offer_id=offer_id,
        keitaro_relation_id=_remote_id(),
        remote_share=Decimal("69.5000"),
    )
    db_session.add_all([first, second])
    db_session.flush()

    assert isinstance(first.id, UUID)
    assert first.id != second.id
    assert first.local_status == second.local_status == LOCAL_OFFER_ACTIVE
    assert [row.id for row in db_session.scalars(
        select(StreamOffer)
        .where(StreamOffer.stream_id == stream.id)
        .order_by(StreamOffer.remote_share)
    )] == [first.id, second.id]


def test_optional_remote_fields_and_local_pin(db_session: Session, stream: Stream):
    position = StreamOffer(
        stream_id=stream.id,
        keitaro_offer_id=_remote_id(),
        keitaro_relation_id=None,
        remote_share=None,
        remote_state=None,
        pinned_percent=25,
    )
    db_session.add(position)
    db_session.flush()
    assert position.keitaro_relation_id is None
    assert position.remote_share is None
    assert position.pinned_percent == 25
    assert position.last_seen_at is None


def test_remote_share_retains_decimal_precision(db_session: Session, stream: Stream):
    value = Decimal("33.333333333333")
    position = StreamOffer(
        stream_id=stream.id,
        keitaro_offer_id=_remote_id(),
        remote_share=value,
    )
    db_session.add(position)
    db_session.flush()
    db_session.expire(position)
    assert position.remote_share == value


def test_archive_keeps_remote_observation_and_local_id(
    db_session: Session, stream: Stream
):
    position = StreamOffer(
        stream_id=stream.id,
        keitaro_offer_id=_remote_id(),
        remote_share=Decimal("50.2500"),
        remote_state="active",
        local_status=LOCAL_OFFER_ACTIVE,
    )
    db_session.add(position)
    db_session.flush()
    position_id = position.id

    position.local_status = LOCAL_OFFER_REMOVED
    db_session.flush()
    db_session.expire(position)
    assert position.id == position_id
    assert position.remote_share == Decimal("50.2500")
    assert position.remote_state == "active"
    assert position.local_status == LOCAL_OFFER_REMOVED

    position.local_status = LOCAL_OFFER_ACTIVE
    db_session.flush()
    assert position.id == position_id
    assert position.local_status == LOCAL_OFFER_ACTIVE


def test_raw_insert_uses_default_active_status(db_session: Session, stream: Stream):
    position_id = uuid4()
    # Raw SQL bypasses SQLAlchemy's Python defaults: verify the DB default.
    db_session.execute(
        text(
            "INSERT INTO kt_stream_offers (id, stream_id, keitaro_offer_id) "
            "VALUES (:id, :stream_id, :keitaro_offer_id)"
        ),
        {"id": position_id, "stream_id": stream.id, "keitaro_offer_id": _remote_id()},
    )
    assert db_session.get(StreamOffer, position_id).local_status == LOCAL_OFFER_ACTIVE


@pytest.mark.parametrize("bad_offer_id", [0, -4])
def test_reject_nonpositive_global_offer_id(
    db_session: Session, stream: Stream, bad_offer_id: int
):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                StreamOffer(stream_id=stream.id, keitaro_offer_id=bad_offer_id)
            )
            db_session.flush()


@pytest.mark.parametrize("bad_relation_id", [0, -3])
def test_reject_nonpositive_relation_id(
    db_session: Session, stream: Stream, bad_relation_id: int
):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                StreamOffer(
                    stream_id=stream.id,
                    keitaro_offer_id=_remote_id(),
                    keitaro_relation_id=bad_relation_id,
                )
            )
            db_session.flush()


def test_reject_negative_remote_share(db_session: Session, stream: Stream):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                StreamOffer(
                    stream_id=stream.id,
                    keitaro_offer_id=_remote_id(),
                    remote_share=Decimal("-0.0001"),
                )
            )
            db_session.flush()


@pytest.mark.parametrize("bad_status", ["DISABLED", "", "ARCHIVED"])
def test_reject_unknown_local_status(
    db_session: Session, stream: Stream, bad_status: str
):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                StreamOffer(
                    stream_id=stream.id,
                    keitaro_offer_id=_remote_id(),
                    local_status=bad_status,
                )
            )
            db_session.flush()


@pytest.mark.parametrize("bad_pin", [-1, 101])
def test_reject_out_of_range_pin(
    db_session: Session, stream: Stream, bad_pin: int
):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                StreamOffer(
                    stream_id=stream.id,
                    keitaro_offer_id=_remote_id(),
                    pinned_percent=bad_pin,
                )
            )
            db_session.flush()


@pytest.mark.parametrize("boundary_pin", [0, 100])
def test_accept_valid_pin_boundary(
    db_session: Session, stream: Stream, boundary_pin: int
):
    position = StreamOffer(
        stream_id=stream.id,
        keitaro_offer_id=_remote_id(),
        pinned_percent=boundary_pin,
    )
    db_session.add(position)
    db_session.flush()
    assert position.pinned_percent == boundary_pin


def test_reject_orphan_stream(db_session: Session):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                StreamOffer(stream_id=uuid4(), keitaro_offer_id=_remote_id())
            )
            db_session.flush()


def test_prevent_deleting_stream_with_offer_positions(
    db_session: Session, stream: Stream
):
    db_session.add(StreamOffer(stream_id=stream.id, keitaro_offer_id=_remote_id()))
    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(delete(Stream).where(Stream.id == stream.id))
