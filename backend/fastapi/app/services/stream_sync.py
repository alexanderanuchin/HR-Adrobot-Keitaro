"""Capture campaign streams and reconcile their local offer positions."""

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import product
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.integrations.keitaro_admin import (
    KeitaroRecord,
    KeitaroResponseError,
    StreamOfferAssignment,
    stream_offer_assignments,
)
from app.models import Campaign, Stream, StreamOffer, StreamSnapshot
from app.models.snapshot import snapshot_fingerprint
from app.models.stream_offer import LOCAL_OFFER_ACTIVE, LOCAL_OFFER_REMOVED
from app.services.draft_workspace import InitialOfferPosition

OFFER_SCHEMAS = {"landings", "offers"}
MAX_REMOTE_ID = 2**63 - 1
NULLABLE_ROUTING_TEXT = ("action_payload", "comments")


@dataclass(frozen=True, slots=True)
class ObservedOffer:
    assignment: StreamOfferAssignment
    relation_id: int | None


def remote_id(value: object) -> int | None:
    if type(value) is int and 0 < value <= MAX_REMOTE_ID:
        return value
    return None


def observed_offers(record: KeitaroRecord) -> list[ObservedOffer]:
    assignments = stream_offer_assignments(record)
    observations = []
    seen_ids: set[int] = set()
    for assignment, row in zip(assignments, record.raw["offers"], strict=True):
        if "stream_id" in row and remote_id(row["stream_id"]) != record.id:
            raise KeitaroResponseError("Keitaro returned an offer association for a different stream")
        relation_id = remote_id(row.get("id"))
        if relation_id is not None:
            if relation_id in seen_ids:
                raise KeitaroResponseError("Keitaro returned a duplicate association ID")
            seen_ids.add(relation_id)
        observations.append(ObservedOffer(assignment, relation_id))
    return observations


def _routing_fields(record: KeitaroRecord) -> dict[str, object]:
    keys = (
        "campaign_id", "name", "position", "weight", "type", "schema", "state",
        "action_type", "action_payload", "action_options", "landings", "filters",
        "filter_or", "collect_clicks", "triggers", "offer_selection", "comments",
    )
    return {key: record.raw[key] for key in keys if key in record.raw}


def routing_fingerprint(record: KeitaroRecord) -> str:
    """Compare routing values, treating empty comments and destinations consistently."""
    fields = _routing_fields(record)
    for key in NULLABLE_ROUTING_TEXT:
        if fields.get(key) == "":
            fields[key] = None
    return snapshot_fingerprint(fields)


def routing_matches(record: KeitaroRecord, fingerprint: str) -> bool:
    if routing_fingerprint(record) == fingerprint:
        return True
    # A publication already in progress can carry a pre-normalization hash.
    # Compare alternate encodings for these specific empty text fields only.
    fields = _routing_fields(record)
    empty = [key for key in NULLABLE_ROUTING_TEXT if key in fields and fields[key] in (None, "")]
    return any(
        snapshot_fingerprint({**fields, **dict(zip(empty, values, strict=True))}) == fingerprint
        for values in product((None, ""), repeat=len(empty))
    )


def stream_payload(record: KeitaroRecord | None) -> tuple[dict[str, object], list[ObservedOffer]]:
    if record is None:
        return {"present": False}, []
    schema = record.raw.get("schema")
    stream_type = record.raw.get("type")
    if not isinstance(schema, str) or not schema or not isinstance(stream_type, str) or not stream_type:
        raise KeitaroResponseError("Keitaro stream needs a schema and type")
    present = record.raw.get("state") != "deleted"
    payload: dict[str, object] = {
        "present": present,
        "name": record.name[:255],
        "schema": "offers" if schema in OFFER_SCHEMAS else schema[:32],
        "type": stream_type[:32],
        "position": record.raw.get("position") if type(record.raw.get("position")) is int else None,
        "state": record.raw.get("state") if isinstance(record.raw.get("state"), str) else None,
    }
    if not present or schema not in OFFER_SCHEMAS:
        return payload, []
    offers = observed_offers(record)
    payload["routing"] = routing_fingerprint(record)
    payload["offers"] = [
        {"id": item.relation_id, "offer_id": item.assignment.offer_id,
         "share": item.assignment.share, "state": item.assignment.state}
        for item in offers
    ]
    return payload, offers


def latest_snapshot(session: Session, stream_id: UUID) -> StreamSnapshot | None:
    return session.scalar(
        select(StreamSnapshot).where(StreamSnapshot.stream_id == stream_id)
        .order_by(StreamSnapshot.captured_at.desc(), StreamSnapshot.id.desc()).limit(1)
    )


def same_offers(old: StreamSnapshot, new: StreamSnapshot) -> bool:
    keys = ("present", "schema", "offers")
    return all(old.normalized_payload.get(key) == new.normalized_payload.get(key) for key in keys)


def offer_positions(session: Session, stream_id: UUID) -> list[StreamOffer]:
    return list(session.scalars(
        select(StreamOffer).where(StreamOffer.stream_id == stream_id)
        .order_by(StreamOffer.local_status, StreamOffer.position_index, StreamOffer.id)
    ))


def _normalized_shares(positions: list[StreamOffer]) -> dict[UUID, int]:
    active = [row for row in positions if row.local_status == LOCAL_OFFER_ACTIVE]
    if not active:
        return {}
    shares = [int(row.remote_share or 0) for row in active]
    if not any(shares):
        shares = [1] * len(active)
    total = sum(shares)
    bases = [divmod(share * 100, total) for share in shares]
    remainder = 100 - sum(base for base, _ in bases)
    priority = sorted(range(len(active)), key=lambda index: -bases[index][1])
    extra = set(priority[:remainder])
    return {row.id: bases[index][0] + (index in extra) for index, row in enumerate(active)}


def initial_positions(session: Session, stream_id: UUID) -> tuple[InitialOfferPosition, ...]:
    rows = offer_positions(session, stream_id)
    shares = _normalized_shares(rows)
    return tuple(InitialOfferPosition(row.id, index, shares.get(row.id, 0)) for index, row in enumerate(rows))


def record_positions(
    session: Session,
    stream: Stream,
    observations: list[ObservedOffer],
    *,
    confirmed: dict[StreamOfferAssignment, deque[StreamOffer]] | None = None,
) -> None:
    """Keep association IDs stable and archive any positions absent from Keitaro."""
    rows = offer_positions(session, stream.id)
    # Prefer a current row if an older archived row once used the same relation.
    by_relation = {}
    without_relation = defaultdict(deque)
    by_offer = defaultdict(deque)
    for row in rows:
        if row.keitaro_relation_id is not None:
            by_relation.setdefault((row.keitaro_relation_id, row.keitaro_offer_id), row)
        elif row.remote_state is not None:
            without_relation[row.keitaro_offer_id].append(row)
        if row.remote_state is not None:
            by_offer[row.keitaro_offer_id].append(row)

    # Reserve exact association matches before considering ID-less observations.
    matches = {
        index: by_relation[(item.relation_id, item.assignment.offer_id)]
        for index, item in enumerate(observations)
        if (item.relation_id, item.assignment.offer_id) in by_relation
    }
    reserved = {row.id for row in matches.values()}
    seen = set()
    now = datetime.now(UTC)
    for index, item in enumerate(observations):
        assignment = item.assignment
        if confirmed is not None:
            row = confirmed[assignment].popleft()
        else:
            row = matches.get(index)
            if row is None:
                queue = (by_offer if item.relation_id is None else without_relation)[assignment.offer_id]
                while queue and (queue[0].id in reserved or queue[0].id in seen):
                    queue.popleft()
                row = queue.popleft() if queue else StreamOffer(
                    stream_id=stream.id, keitaro_offer_id=assignment.offer_id,
                )
        session.add(row)
        row.keitaro_relation_id = item.relation_id
        row.remote_share = assignment.share
        row.remote_state = assignment.state
        row.position_index = index
        row.local_status = LOCAL_OFFER_ACTIVE if assignment.state == "active" else LOCAL_OFFER_REMOVED
        row.last_seen_at = now
        seen.add(row.id)

    for row in rows:
        if row.id not in seen:
            row.local_status = LOCAL_OFFER_REMOVED
            row.remote_state = None
    session.flush()
    rows = offer_positions(session, stream.id)
    shares = _normalized_shares(rows)
    for row in rows:
        # An external edit can invalidate an old local PIN. Archived rows keep
        # their PIN so BRING BACK can use it and run the normal weight check.
        if row.id in shares and row.pinned_percent != shares[row.id]:
            row.pinned_percent = None


def capture(
    session: Session,
    stream: Stream,
    record: KeitaroRecord | None,
    *,
    confirmed: dict[StreamOfferAssignment, deque[StreamOffer]] | None = None,
) -> StreamSnapshot:
    payload, observations = stream_payload(record)
    fingerprint = snapshot_fingerprint(payload)
    latest = latest_snapshot(session, stream.id)
    stream.last_synced_at = datetime.now(UTC)
    if record is not None:
        stream.name = record.name[:255]
        stream.stream_type = payload["type"]
        stream.schema_type = payload["schema"]
    if latest is not None and latest.fingerprint == fingerprint and confirmed is None:
        return latest
    if latest is None or latest.fingerprint != fingerprint:
        latest = StreamSnapshot.capture(stream_id=stream.id, normalized_payload=payload)
        session.add(latest)
    record_positions(session, stream, observations, confirmed=confirmed)
    session.flush()
    return latest


def record_campaign(session: Session, record: KeitaroRecord) -> Campaign:
    session.execute(insert(Campaign).values(
        keitaro_campaign_id=record.id, name=record.name[:255],
    ).on_conflict_do_nothing(index_elements=[Campaign.keitaro_campaign_id]))
    campaign = session.scalar(
        select(Campaign).where(Campaign.keitaro_campaign_id == record.id).with_for_update()
    )
    campaign.name = record.name[:255]
    if "domain_id" in record.raw:
        campaign.domain_id = remote_id(record.raw["domain_id"])
    campaign.group_id = remote_id(record.raw.get("group_id"))
    campaign.traffic_source_id = remote_id(record.raw.get("traffic_source_id"))
    campaign.last_synced_at = datetime.now(UTC)
    session.flush()
    return campaign
