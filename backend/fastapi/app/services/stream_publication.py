"""Save, reconcile and publish the editor's offer changes."""

from collections import Counter, defaultdict, deque
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.workspace import operation_session
from app.integrations.keitaro_admin import (
    KeitaroAdminClient,
    KeitaroError,
    KeitaroHTTPError,
    KeitaroNotFoundError,
    KeitaroRecord,
    KeitaroResponseError,
    StreamOfferAssignment,
)
from app.models import Campaign, DraftOffer, Stream, StreamDraft, StreamOffer, StreamSnapshot
from app.models.draft import (
    DRAFT_CANCELLED, DRAFT_CONFLICT, DRAFT_DIRTY, DRAFT_PUBLISHED,
    DRAFT_PUBLISHING, DRAFT_SYNCED, DRAFT_UNCERTAIN, DRAFT_VERIFYING, TERMINAL_DRAFT_STATUSES,
)
from app.models.draft_offer import DRAFT_OFFER_ACTIVE
from app.services.draft_editor import DraftEditBlocked, DraftEditError, DraftRevisionConflict
from app.services.draft_workspace import (
    DraftWorkspaceNotFound, DraftWorkspaceView, open_or_create_draft_workspace, read_draft_workspace,
)
from app.services.stream_sync import (
    capture, initial_positions, latest_snapshot, observed_offers, offer_positions,
    record_campaign, remote_id, routing_fingerprint, routing_matches, same_offers,
)

RECOVERABLE_STATES = frozenset({DRAFT_PUBLISHING, DRAFT_VERIFYING, DRAFT_UNCERTAIN})


def lock_stream(session: Session, stream_id: UUID) -> Stream:
    stream = session.scalar(select(Stream).where(Stream.id == stream_id).with_for_update())
    if stream is None:
        raise DraftWorkspaceNotFound("Stream has not been fetched")
    return stream


def unfinished_draft(session: Session, stream_id: UUID) -> StreamDraft | None:
    return session.scalar(select(StreamDraft).where(
        StreamDraft.stream_id == stream_id, StreamDraft.status.not_in(TERMINAL_DRAFT_STATUSES),
    ).with_for_update())


def is_editable(snapshot: StreamSnapshot | None) -> bool:
    if snapshot is None:
        return False
    payload = snapshot.normalized_payload
    return payload.get("present") is True and payload.get("schema") == "offers"


def workspace(session: Session, stream_id: UUID, actor_id: int):
    stream = lock_stream(session, stream_id)
    draft = unfinished_draft(session, stream.id)
    if draft is not None:
        return read_draft_workspace(session, draft.id)
    latest = latest_snapshot(session, stream.id)
    if latest is None or not is_editable(latest):
        raise DraftEditBlocked("This flow does not have an editable offer configuration")
    return open_or_create_draft_workspace(
        session, stream_id=stream.id, base_snapshot_id=latest.id, created_by_id=actor_id,
        initial_positions=initial_positions(session, stream.id),
    )


def _check_revision(draft: StreamDraft | None, draft_id: UUID, revision: int) -> StreamDraft:
    if draft is None or draft.id != draft_id or draft.revision != revision:
        raise DraftRevisionConflict("The draft has changed. Reload the workspace.")
    return draft


def cancel_draft(
    session: Session, draft_id: UUID, revision: int, actor_id: int, *, confirm_uncertain: bool = False,
):
    stream_id = session.scalar(select(StreamDraft.stream_id).where(StreamDraft.id == draft_id))
    if stream_id is None:
        raise DraftWorkspaceNotFound("Draft not found")
    stream = lock_stream(session, stream_id)
    draft = _check_revision(unfinished_draft(session, stream.id), draft_id, revision)
    if draft.status in {DRAFT_PUBLISHING, DRAFT_VERIFYING} or (
        draft.status == DRAFT_UNCERTAIN
        and not (confirm_uncertain and (draft.conflict_details or {}).get("checked"))
    ):
        raise DraftEditBlocked("Fetch Keitaro to reconcile the previous publication before discarding")
    draft.status = DRAFT_CANCELLED
    draft.revision += 1
    session.flush()
    if is_editable(latest_snapshot(session, stream.id)):
        return workspace(session, stream.id, actor_id)
    return None


def _owned_detail(client: KeitaroAdminClient, stream: Stream, campaign_id: int) -> KeitaroRecord | None:
    try:
        record = client.get_stream(stream.keitaro_stream_id)
    except KeitaroNotFoundError:
        return None
    if remote_id(record.raw.get("campaign_id")) != campaign_id:
        raise KeitaroResponseError("Keitaro returned a stream for a different campaign")
    return record


def publication_assignments(session: Session, view: DraftWorkspaceView) -> list[dict]:
    source = {row.id: row for row in offer_positions(session, view.stream_id)}
    active_offer_ids = {row.keitaro_offer_id for row in view.positions if row.status == DRAFT_OFFER_ACTIVE}
    disabled_offer_ids: set[int] = set()
    assignments = []
    for row in view.positions:
        observed = source.get(row.source_position_id)
        if row.status == DRAFT_OFFER_ACTIVE:
            share, state = row.calculated_percent, "active"
        elif (
            row.keitaro_offer_id not in active_offer_ids
            and row.keitaro_offer_id not in disabled_offer_ids
            and observed is not None
            and observed.remote_state == "disabled"
            and observed.remote_share is not None
        ):
            # A disabled association can be kept only once, and an active position takes precedence.
            share, state = int(observed.remote_share), "disabled"
            disabled_offer_ids.add(row.keitaro_offer_id)
        else:
            continue
        assignments.append({"position": str(row.position_id), "offer_id": row.keitaro_offer_id,
                            "share": share, "state": state})
    return assignments


def duplicate_offer_message(assignments: list[dict]) -> str | None:
    counts = Counter(item["offer_id"] for item in assignments)
    duplicates = sorted(offer_id for offer_id, count in counts.items() if count > 1)
    if not duplicates:
        return None
    ids = ", ".join(f"#{offer_id}" for offer_id in duplicates)
    return f"Офферы с ID {ids} повторяются. Перед публикацией оставьте одну активную строку для каждого ID."


def _assignment(item: dict) -> StreamOfferAssignment:
    return StreamOfferAssignment(item["offer_id"], item["share"], item["state"])


def _matches(record: KeitaroRecord | None, intent: dict) -> bool:
    return (
        record is not None
        and routing_matches(record, intent["routing"])
        and Counter(item.assignment for item in observed_offers(record))
        == Counter(_assignment(item) for item in intent["assignments"])
    )


def _confirm(session: Session, stream: Stream, draft: StreamDraft, record: KeitaroRecord, intent: dict):
    positions = read_draft_workspace(session, draft.id).positions
    source = {row.id: row for row in offer_positions(session, stream.id)}
    by_position = {}
    for index, position in enumerate(positions):
        row = source.get(position.source_position_id)
        if row is None:
            row = StreamOffer(
                stream_id=stream.id, keitaro_offer_id=position.keitaro_offer_id,
                local_status="REMOVED", position_index=index,
            )
            session.add(row)
        row.pinned_percent = position.pinned_percent
        row.pin_immediately = position.pin_immediately
        row.pending_pinned_percent = position.pending_pinned_percent
        by_position[str(position.position_id)] = row
    session.flush()
    matched = defaultdict(deque)
    for item in intent["assignments"]:
        matched[_assignment(item)].append(by_position[item["position"]])
    latest = capture(session, stream, record, confirmed=matched)
    draft.latest_remote_snapshot_id = latest.id
    draft.status = DRAFT_PUBLISHED
    draft.conflict_details = None
    draft.revision += 1
    session.flush()
    return read_draft_workspace(session, draft.id)


def _reconcile(session: Session, stream: Stream, record: KeitaroRecord | None) -> None:
    draft = unfinished_draft(session, stream.id)
    if draft is not None and (
        draft.status in RECOVERABLE_STATES
        or draft.status == DRAFT_CONFLICT and (draft.conflict_details or {}).get("intent")
    ):
        details = draft.conflict_details or {}
        intent = details.get("intent")
        if intent and _matches(record, intent):
            _confirm(session, stream, draft, record, intent)
            return
        latest = capture(session, stream, record)
        # A fresh observation showing a different offer list is a conflict.
        # If the original list is still visible, a timed-out request may still
        # complete remotely. Do not make that draft eligible for another PUT.
        base = session.get(StreamSnapshot, draft.base_snapshot_id)
        status = DRAFT_UNCERTAIN if same_offers(base, latest) else DRAFT_CONFLICT
        if draft.status != status or draft.latest_remote_snapshot_id != latest.id or not details.get("checked"):
            draft.status = status
            draft.latest_remote_snapshot_id = latest.id
            draft.conflict_details = {**details, "reason": "publication_unconfirmed", "checked": True}
            draft.revision += 1
        return

    latest = capture(session, stream, record)
    if draft is None or draft.latest_remote_snapshot_id == latest.id:
        return
    base = session.get(StreamSnapshot, draft.base_snapshot_id)
    if base.id == latest.id:
        return
    draft.latest_remote_snapshot_id = latest.id
    if same_offers(base, latest):
        # Keep local edits when only routing metadata changed. The subsequent
        # PUT touches offers only and readback checks the fresh routing state.
        draft.base_snapshot_id = latest.id
        if draft.status == DRAFT_CONFLICT and not (draft.conflict_details or {}).get("intent"):
            draft.status = DRAFT_DIRTY
            draft.conflict_details = None
    elif draft.status == DRAFT_SYNCED:
        draft.status = DRAFT_CANCELLED
    else:
        draft.status = DRAFT_CONFLICT
        draft.conflict_details = {"reason": "remote_offers_changed"}
    draft.revision += 1


def sync_campaign(engine: Engine, client: KeitaroAdminClient, campaign_id: int) -> None:
    with operation_session(engine, f"campaign:{campaign_id}") as session, session.begin():
        remote_campaign = client.get_campaign(campaign_id)
        records = client.list_streams(campaign_id)
        by_id = {record.id: record for record in records}
        if len(by_id) != len(records):
            raise KeitaroResponseError("Keitaro returned duplicate streams")
        campaign = record_campaign(session, remote_campaign)
        streams = {stream.keitaro_stream_id: stream for stream in session.scalars(
            select(Stream).where(Stream.campaign_id == campaign.id).order_by(Stream.id).with_for_update()
        )}
        for remote_id_value in dict.fromkeys([*by_id, *streams]):
            record = by_id.get(remote_id_value)
            stream = streams.get(remote_id_value)
            if stream is None:
                stream = session.scalar(
                    select(Stream).where(Stream.keitaro_stream_id == remote_id_value).with_for_update()
                )
                if stream is not None:
                    raise KeitaroResponseError("Keitaro stream ownership changed")
                stream = Stream(campaign_id=campaign.id, keitaro_stream_id=remote_id_value,
                                name="", stream_type="", schema_type="")
                session.add(stream)
                session.flush()
            pending = unfinished_draft(session, stream.id)
            if record is not None and pending is not None and (
                pending.status in RECOVERABLE_STATES or (pending.conflict_details or {}).get("intent")
            ):
                record = _owned_detail(client, stream, campaign_id)
            _reconcile(session, stream, record)


def publish_draft(
    engine: Engine, client: KeitaroAdminClient, draft_id: UUID, revision: int,
):
    with Session(engine) as reader:
        target = reader.execute(select(Stream.id, Campaign.keitaro_campaign_id).join(
            StreamDraft, StreamDraft.stream_id == Stream.id,
        ).join(Campaign, Campaign.id == Stream.campaign_id).where(StreamDraft.id == draft_id)).first()
    if target is None:
        raise DraftWorkspaceNotFound("Draft not found")
    stream_id, campaign_id = target

    with operation_session(engine, f"campaign:{campaign_id}") as session:
        with session.begin():
            stream = lock_stream(session, stream_id)
            previous = session.get(StreamDraft, draft_id, with_for_update=True)
            if previous is not None and previous.status == DRAFT_PUBLISHED:
                return read_draft_workspace(session, draft_id)
            if previous is not None and previous.status in TERMINAL_DRAFT_STATUSES:
                previous = None
            draft = _check_revision(previous, draft_id, revision)
            if draft.status != DRAFT_DIRTY:
                raise DraftEditBlocked("Only a draft with pending edits can be published")
            before = _owned_detail(client, stream, campaign_id)
            _reconcile(session, stream, before)
            if draft.status == DRAFT_CONFLICT:
                return read_draft_workspace(session, draft.id)
            if before is None or not is_editable(latest_snapshot(session, stream.id)):
                raise DraftEditBlocked("The original flow is unavailable")
            assignments = publication_assignments(session, read_draft_workspace(session, draft.id))
            if message := duplicate_offer_message(assignments):
                raise DraftEditError(message)
            intent = {"routing": routing_fingerprint(before), "assignments": assignments}
            if _matches(before, intent):
                return _confirm(session, stream, draft, before, intent)
            draft.status = DRAFT_PUBLISHING
            draft.revision += 1
            draft.conflict_details = {"intent": intent, "reason": "publication_in_progress"}

        write_error = None
        try:
            client.replace_stream_offers(stream.keitaro_stream_id, [
                _assignment(item) for item in intent["assignments"]
            ])
        except KeitaroError as exc:
            write_error = exc

        with session.begin():
            stream = lock_stream(session, stream_id)
            draft = unfinished_draft(session, stream.id)
            try:
                observed = _owned_detail(client, stream, campaign_id)
                if _matches(observed, intent):
                    return _confirm(session, stream, draft, observed, intent)
                latest = capture(session, stream, observed)
                draft.latest_remote_snapshot_id = latest.id
            except KeitaroError:
                latest = None
            base = session.get(StreamSnapshot, draft.base_snapshot_id)
            if (
                isinstance(write_error, KeitaroHTTPError)
                and write_error.status_code in {400, 401, 402, 403, 404, 406, 422, 429}
                and latest is not None
                and same_offers(base, latest)
            ):
                draft.status = DRAFT_DIRTY
                draft.conflict_details = {"reason": "publication_rejected", "status_code": write_error.status_code}
            else:
                draft.status = DRAFT_UNCERTAIN
                draft.conflict_details = {"intent": intent, "reason": "publication_unconfirmed"}
            draft.revision += 1
            session.flush()
            return read_draft_workspace(session, draft.id)
