"""A-02 snapshot tests: deterministic hashes, persistence, immutable history."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import Base
from app.models import Campaign, Stream, StreamSnapshot
from app.models.snapshot import canonical_snapshot_json, snapshot_fingerprint


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
def persisted_stream(db_session: Session) -> Stream:
    campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Snapshot campaign")
    stream = Stream(
        keitaro_stream_id=_remote_id(),
        name="Offers",
        stream_type="default",
        schema_type="offers",
    )
    campaign.streams.append(stream)
    db_session.add(campaign)
    db_session.flush()
    return stream


def test_snapshot_table_is_registered_without_django_tables():
    assert "kt_stream_snapshots" in Base.metadata.tables
    assert all(name.startswith("kt_") for name in Base.metadata.tables)


def test_canonical_fingerprint_is_independent_of_object_key_order():
    first = {"name": "Поток", "offers": [{"id": 7, "weight": 25}], "nested": {"z": 2, "a": 1}}
    second = {"nested": {"a": 1, "z": 2}, "offers": [{"weight": 25, "id": 7}], "name": "Поток"}
    assert canonical_snapshot_json(first) == canonical_snapshot_json(second)
    assert snapshot_fingerprint(first) == snapshot_fingerprint(second)
    assert len(snapshot_fingerprint(first)) == 64


def test_invalid_snapshot_payload_is_rejected_early():
    with pytest.raises(TypeError):
        canonical_snapshot_json([1, 2])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        canonical_snapshot_json({"weight": float("nan")})


def test_capture_copies_input_and_keeps_a_matching_fingerprint():
    original = {"offers": [{"id": 5, "weight": 100}]}
    snapshot = StreamSnapshot.capture(stream_id=uuid4(), normalized_payload=original)
    original["offers"][0]["weight"] = 0
    assert snapshot.normalized_payload == {"offers": [{"id": 5, "weight": 100}]}
    assert snapshot.fingerprint == snapshot_fingerprint(snapshot.normalized_payload)


def test_persist_two_equal_captures_without_losing_history(
    db_session: Session, persisted_stream: Stream
):
    payload = {"offers": [{"id": 12, "weight": 100}], "filters": []}
    first = StreamSnapshot.capture(stream_id=persisted_stream.id, normalized_payload=payload)
    second = StreamSnapshot.capture(stream_id=persisted_stream.id, normalized_payload=payload)
    db_session.add_all([first, second])
    db_session.flush()

    assert first.id != second.id
    assert first.fingerprint == second.fingerprint
    assert first.captured_at is not None
    assert second.captured_at is not None
    assert set(db_session.scalars(select(StreamSnapshot).where(StreamSnapshot.stream_id == persisted_stream.id)).all()) == {first, second}


def test_snapshot_rejects_incorrect_fingerprint(
    db_session: Session, persisted_stream: Stream
):
    snapshot = StreamSnapshot.capture(
        stream_id=persisted_stream.id, normalized_payload={"offers": []}
    )
    snapshot.fingerprint = "0" * 64
    with pytest.raises(ValueError, match="fingerprint"):
        with db_session.begin_nested():
            db_session.add(snapshot)
            db_session.flush()


def test_snapshot_rejects_orphan_stream(db_session: Session):
    snapshot = StreamSnapshot.capture(
        stream_id=uuid4(), normalized_payload={"offers": []}
    )
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(snapshot)
            db_session.flush()


def test_database_enforces_object_payload_and_sha256_format(
    db_session: Session, persisted_stream: Stream
):
    bad_fields = [
        {"normalized_payload": ["not a JSON object"], "fingerprint": "f" * 64},
        {"normalized_payload": {"offers": []}, "fingerprint": "invalid"},
    ]
    for item in bad_fields:
        with pytest.raises(IntegrityError):
            with db_session.begin_nested():
                db_session.execute(
                    insert(StreamSnapshot).values(
                        id=uuid4(), stream_id=persisted_stream.id, **item
                    )
                )


def test_database_blocks_update_and_delete_of_snapshot(
    db_session: Session, persisted_stream: Stream
):
    snapshot = StreamSnapshot.capture(
        stream_id=persisted_stream.id, normalized_payload={"offers": []}
    )
    db_session.add(snapshot)
    db_session.flush()

    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            db_session.execute(
                update(StreamSnapshot)
                .where(StreamSnapshot.id == snapshot.id)
                .values(fingerprint="f" * 64)
            )

    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            db_session.execute(delete(StreamSnapshot).where(StreamSnapshot.id == snapshot.id))

    assert db_session.get(StreamSnapshot, snapshot.id) is not None


def test_stream_cannot_be_deleted_while_snapshot_exists(
    db_session: Session, persisted_stream: Stream
):
    db_session.add(
        StreamSnapshot.capture(
            stream_id=persisted_stream.id, normalized_payload={"offers": []}
        )
    )
    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(delete(Stream).where(Stream.id == persisted_stream.id))
