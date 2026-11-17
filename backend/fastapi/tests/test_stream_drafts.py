"""A-02/A-04 draft metadata tests; database cases require Alembic 0005."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import Base
from app.models import Campaign, Stream, StreamDraft, StreamSnapshot
from app.models.draft import (
    DRAFT_CANCELLED,
    DRAFT_CONFLICT,
    DRAFT_DIRTY,
    DRAFT_FAILED,
    DRAFT_PUBLISHED,
    DRAFT_PUBLISHING,
    DRAFT_STATUSES,
    DRAFT_SYNCED,
    DRAFT_UNCERTAIN,
    DRAFT_VERIFYING,
    TERMINAL_DRAFT_STATUSES,
)


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
    campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Draft test")
    stream = Stream(
        keitaro_stream_id=_remote_id(),
        name="Stream for draft",
        stream_type="default",
        schema_type="offers",
    )
    campaign.streams.append(stream)
    db_session.add(campaign)
    db_session.flush()
    return stream


def _snapshot(session: Session, stream: Stream) -> StreamSnapshot:
    row = StreamSnapshot.capture(
        stream_id=stream.id, normalized_payload={"offers": [], "sequence": _remote_id()}
    )
    session.add(row)
    session.flush()
    return row


def _draft(stream: Stream, snapshot: StreamSnapshot, **kwargs) -> StreamDraft:
    return StreamDraft(
        stream_id=stream.id,
        base_snapshot_id=snapshot.id,
        created_by_id=1,
        **kwargs,
    )


def test_metadata_tables_and_status_vocabulary():
    assert "kt_stream_drafts" in Base.metadata.tables
    assert all(name.startswith("kt_") for name in Base.metadata.tables)
    assert DRAFT_STATUSES == {
        "SYNCED", "DIRTY", "CONFLICT", "PUBLISHING", "VERIFYING",
        "UNCERTAIN", "FAILED", "PUBLISHED", "CANCELLED",
    }
    assert TERMINAL_DRAFT_STATUSES == {DRAFT_PUBLISHED, DRAFT_CANCELLED}


def test_draft_has_only_a_local_django_user_id_and_no_django_fk():
    assert not StreamDraft.__table__.c.created_by_id.foreign_keys
    assert StreamDraft.__table__.c.stream_id.foreign_keys
    assert len(StreamDraft.__table__.c.base_snapshot_id.foreign_keys) == 1
    assert len(StreamDraft.__table__.c.latest_remote_snapshot_id.foreign_keys) == 1


def test_one_unfinished_draft_uses_partial_unique_index():
    index = next(
        index for index in StreamDraft.__table__.indexes
        if index.name == "uq_kt_stream_drafts_one_unfinished_per_stream"
    )
    assert index.unique
    assert {column.name for column in index.columns} == {"stream_id"}
    expression = str(index.dialect_options["postgresql"]["where"])
    assert "PUBLISHED" in expression and "CANCELLED" in expression


def test_insert_draft_with_base_snapshot(db_session: Session, persisted_stream: Stream):
    base = _snapshot(db_session, persisted_stream)
    draft = _draft(persisted_stream, base)
    db_session.add(draft)
    db_session.flush()

    assert isinstance(draft.id, UUID)
    assert draft.stream.id == persisted_stream.id
    assert draft.status == DRAFT_SYNCED
    assert draft.revision == 0
    assert draft.created_by_id == 1
    assert draft.base_snapshot_id == base.id
    assert draft.latest_remote_snapshot_id is None
    assert draft.created_at is not None
    assert draft.updated_at is not None
    assert db_session.scalar(select(StreamDraft).where(StreamDraft.id == draft.id)) is draft



def test_raw_insert_uses_database_defaults(db_session: Session, persisted_stream: Stream):
    base = _snapshot(db_session, persisted_stream)
    draft_id = uuid4()
    db_session.execute(
        text(
            "INSERT INTO kt_stream_drafts (id, stream_id, base_snapshot_id, created_by_id) "
            "VALUES (:id, :stream_id, :base_snapshot_id, :created_by_id)"
        ),
        {
            "id": draft_id,
            "stream_id": persisted_stream.id,
            "base_snapshot_id": base.id,
            "created_by_id": 1,
        },
    )
    draft = db_session.get(StreamDraft, draft_id)
    assert draft is not None
    assert draft.status == DRAFT_SYNCED
    assert draft.revision == 0
    assert draft.created_at is not None
    assert draft.updated_at is not None


def test_distinct_streams_can_have_unfinished_drafts(db_session: Session, persisted_stream: Stream):
    first_base = _snapshot(db_session, persisted_stream)
    other_campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Other editor")
    other_stream = Stream(
        keitaro_stream_id=_remote_id(), name="Another stream",
        stream_type="default", schema_type="offers",
    )
    other_campaign.streams.append(other_stream)
    db_session.add(other_campaign)
    db_session.flush()
    second_base = _snapshot(db_session, other_stream)
    db_session.add_all([
        _draft(persisted_stream, first_base, status=DRAFT_DIRTY),
        _draft(other_stream, second_base, status=DRAFT_DIRTY),
    ])
    db_session.flush()
    assert len(db_session.scalars(select(StreamDraft).where(StreamDraft.status == DRAFT_DIRTY)).all()) == 2


def test_latest_snapshot_and_conflict_details_are_preserved(
    db_session: Session, persisted_stream: Stream
):
    base = _snapshot(db_session, persisted_stream)
    remote = _snapshot(db_session, persisted_stream)
    draft = _draft(
        persisted_stream,
        base,
        status=DRAFT_CONFLICT,
        latest_remote_snapshot_id=remote.id,
        conflict_details={"fields": ["offers"], "remote_hash": remote.fingerprint},
        revision=5,
    )
    db_session.add(draft)
    db_session.flush()
    db_session.expire(draft)
    assert draft.base_snapshot_id == base.id
    assert draft.latest_remote_snapshot_id == remote.id
    assert draft.conflict_details == {
        "fields": ["offers"], "remote_hash": remote.fingerprint,
    }
    assert draft.revision == 5


def test_reject_draft_of_missing_stream(db_session: Session):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(StreamDraft(stream_id=uuid4(), base_snapshot_id=uuid4(), created_by_id=1))
            db_session.flush()


def test_reject_nonexistent_base_snapshot(db_session: Session, persisted_stream: Stream):
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(StreamDraft(stream_id=persisted_stream.id, base_snapshot_id=uuid4(), created_by_id=1))
            db_session.flush()


def test_reject_base_snapshot_from_another_stream(db_session: Session, persisted_stream: Stream):
    other_campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Other campaign")
    other = Stream(
        keitaro_stream_id=_remote_id(), name="Other stream",
        stream_type="default", schema_type="offers",
    )
    other_campaign.streams.append(other)
    db_session.add(other_campaign)
    db_session.flush()
    wrong_base = _snapshot(db_session, other)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_draft(persisted_stream, wrong_base))
            db_session.flush()


def test_reject_latest_remote_snapshot_from_another_stream(
    db_session: Session, persisted_stream: Stream
):
    base = _snapshot(db_session, persisted_stream)
    other_campaign = Campaign(keitaro_campaign_id=_remote_id(), name="Remote campaign")
    other = Stream(
        keitaro_stream_id=_remote_id(), name="Other remote stream",
        stream_type="default", schema_type="offers",
    )
    other_campaign.streams.append(other)
    db_session.add(other_campaign)
    db_session.flush()
    wrong_remote = _snapshot(db_session, other)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_draft(persisted_stream, base, latest_remote_snapshot_id=wrong_remote.id))
            db_session.flush()


def test_reject_nonexistent_latest_remote_snapshot(
    db_session: Session, persisted_stream: Stream
):
    base = _snapshot(db_session, persisted_stream)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_draft(persisted_stream, base, latest_remote_snapshot_id=uuid4()))
            db_session.flush()


@pytest.mark.parametrize("bad_status", ["", "ARCHIVED", "UNKNOWN"])
def test_reject_unknown_status(db_session: Session, persisted_stream: Stream, bad_status: str):
    base = _snapshot(db_session, persisted_stream)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_draft(persisted_stream, base, status=bad_status))
            db_session.flush()


@pytest.mark.parametrize("bad_revision", [-1, -2])
def test_reject_negative_revision(
    db_session: Session, persisted_stream: Stream, bad_revision: int
):
    base = _snapshot(db_session, persisted_stream)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_draft(persisted_stream, base, revision=bad_revision))
            db_session.flush()


@pytest.mark.parametrize("bad_actor_id", [0, -5])
def test_reject_invalid_creator_id(
    db_session: Session, persisted_stream: Stream, bad_actor_id: int
):
    base = _snapshot(db_session, persisted_stream)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(StreamDraft(
                stream_id=persisted_stream.id,
                base_snapshot_id=base.id,
                created_by_id=bad_actor_id,
            ))
            db_session.flush()


def test_reject_non_object_conflict_json(db_session: Session, persisted_stream: Stream):
    base = _snapshot(db_session, persisted_stream)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(
                insert(StreamDraft).values(
                    id=uuid4(), stream_id=persisted_stream.id,
                    base_snapshot_id=base.id, created_by_id=1,
                    conflict_details=["not", "an", "object"],
                )
            )


def test_prevent_two_unfinished_drafts_for_same_stream(
    db_session: Session, persisted_stream: Stream
):
    base = _snapshot(db_session, persisted_stream)
    db_session.add(_draft(persisted_stream, base, status=DRAFT_DIRTY))
    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_draft(persisted_stream, base, status=DRAFT_SYNCED))
            db_session.flush()


@pytest.mark.parametrize(
    "blocking_status",
    [DRAFT_CONFLICT, DRAFT_PUBLISHING, DRAFT_VERIFYING, DRAFT_UNCERTAIN, DRAFT_FAILED],
)
def test_unresolved_states_block_parallel_draft(
    db_session: Session, persisted_stream: Stream, blocking_status: str
):
    base = _snapshot(db_session, persisted_stream)
    db_session.add(_draft(persisted_stream, base, status=blocking_status))
    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(_draft(persisted_stream, base, status=DRAFT_SYNCED))
            db_session.flush()


@pytest.mark.parametrize("finished_status", [DRAFT_PUBLISHED, DRAFT_CANCELLED])
def test_completed_draft_allows_next_unfinished(
    db_session: Session, persisted_stream: Stream, finished_status: str
):
    base = _snapshot(db_session, persisted_stream)
    db_session.add(_draft(persisted_stream, base, status=finished_status))
    db_session.flush()
    db_session.add(_draft(persisted_stream, base, status=DRAFT_SYNCED))
    db_session.flush()
    drafts = db_session.scalars(select(StreamDraft).where(StreamDraft.stream_id == persisted_stream.id)).all()
    assert len(drafts) == 2
    assert {draft.status for draft in drafts} == {finished_status, DRAFT_SYNCED}


def test_completed_drafts_can_coexist(db_session: Session, persisted_stream: Stream):
    base = _snapshot(db_session, persisted_stream)
    db_session.add_all([
        _draft(persisted_stream, base, status=DRAFT_PUBLISHED),
        _draft(persisted_stream, base, status=DRAFT_CANCELLED),
    ])
    db_session.flush()
    assert db_session.scalar(select(StreamDraft).where(StreamDraft.status == DRAFT_PUBLISHED))


def test_draft_restricts_snapshot_deletion(db_session: Session, persisted_stream: Stream):
    base = _snapshot(db_session, persisted_stream)
    db_session.add(_draft(persisted_stream, base))
    db_session.flush()
    # Prior migration makes every snapshot append-only: the trigger rejects DELETE.
    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            db_session.execute(delete(StreamSnapshot).where(StreamSnapshot.id == base.id))


def test_draft_restricts_stream_deletion(db_session: Session, persisted_stream: Stream):
    base = _snapshot(db_session, persisted_stream)
    db_session.add(_draft(persisted_stream, base))
    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(delete(Stream).where(Stream.id == persisted_stream.id))
