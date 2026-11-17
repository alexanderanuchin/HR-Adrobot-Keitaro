"""Tests for the first A-04 tables. Database tests require applied Alembic."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import Base
from app.models import Campaign, Stream


def _remote_id() -> int:
    # Random positive bigint so tests do not collide with sandbox campaign IDs.
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


def test_registered_models_are_only_kt_tables():
    tables = set(Base.metadata.tables)
    assert {"kt_campaigns", "kt_streams"} <= tables
    assert all(name.startswith("kt_") for name in tables)


def test_stream_has_restrict_foreign_key():
    foreign_keys = Stream.__table__.c.campaign_id.foreign_keys
    assert len(foreign_keys) == 1
    fk = next(iter(foreign_keys))
    assert fk.target_fullname == "kt_campaigns.id"
    assert fk.ondelete == "RESTRICT"


def test_insert_campaign_with_two_streams(db_session: Session):
    campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Test campaign")
    campaign.streams = [
        Stream(
            keitaro_stream_id=_remote_id(),
            name="GEO flow",
            stream_type="regular",
            schema_type="redirect",
        ),
        Stream(
            keitaro_stream_id=_remote_id(),
            name="Fallback",
            stream_type="default",
            schema_type="offers",
        ),
    ]
    db_session.add(campaign)
    db_session.flush()

    assert isinstance(campaign.id, UUID)
    assert campaign.created_at is not None
    assert all(isinstance(s.id, UUID) for s in campaign.streams)
    assert {s.campaign_id for s in campaign.streams} == {campaign.id}
    assert db_session.scalar(
        select(Campaign).where(Campaign.keitaro_campaign_id == campaign.keitaro_campaign_id)
    ) is campaign


def test_campaign_keitaro_id_is_unique(db_session: Session):
    remote_id = _remote_id()
    db_session.add(Campaign(keitaro_campaign_id=remote_id, name="First"))
    db_session.flush()

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(Campaign(keitaro_campaign_id=remote_id, name="Duplicate"))
            db_session.flush()


def test_campaign_remote_id_must_be_positive(db_session: Session):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(Campaign(keitaro_campaign_id=0, name="Invalid"))
            db_session.flush()


def test_stream_keitaro_id_is_unique(db_session: Session):
    campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Unique flow")
    db_session.add(campaign)
    db_session.flush()

    remote_id = _remote_id()
    db_session.add(
        Stream(
            campaign_id=campaign.id,
            keitaro_stream_id=remote_id,
            name="First",
            stream_type="regular",
            schema_type="offers",
        )
    )
    db_session.flush()

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                Stream(
                    campaign_id=campaign.id,
                    keitaro_stream_id=remote_id,
                    name="Duplicate",
                    stream_type="default",
                    schema_type="offers",
                )
            )
            db_session.flush()


def test_stream_must_reference_existing_campaign(db_session: Session):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                Stream(
                    campaign_id=uuid4(),
                    keitaro_stream_id=_remote_id(),
                    name="Orphan flow",
                    stream_type="regular",
                    schema_type="offers",
                )
            )
            db_session.flush()


def test_stream_remote_id_must_be_positive(db_session: Session):
    campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Valid parent")
    db_session.add(campaign)
    db_session.flush()

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                Stream(
                    campaign_id=campaign.id,
                    keitaro_stream_id=0,
                    name="Invalid remote ID",
                    stream_type="regular",
                    schema_type="offers",
                )
            )
            db_session.flush()


def test_campaign_cannot_be_deleted_with_streams(db_session: Session):
    from sqlalchemy import delete

    campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Do not cascade")
    campaign.streams = [
        Stream(
            keitaro_stream_id=_remote_id(),
            name="Existing flow",
            stream_type="default",
            schema_type="offers",
        )
    ]
    db_session.add(campaign)
    db_session.flush()

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(delete(Campaign).where(Campaign.id == campaign.id))
