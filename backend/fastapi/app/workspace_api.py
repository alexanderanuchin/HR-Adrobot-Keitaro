"""Authenticated internal endpoints consumed by the Django gateway."""

from collections.abc import Iterator
from dataclasses import asdict
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.workspace import get_workspace_engine
from app.integrations.keitaro_admin import KeitaroAdminClient, KeitaroRecord
from app.models import Campaign, Stream, StreamDraft
from app.models.draft import DRAFT_DIRTY, TERMINAL_DRAFT_STATUSES
from app.services.campaign_preflight import CampaignCreationRequest
from app.services.draft_editor import (
    add_offer_to_draft, bring_back_offer_in_draft, pin_offer_in_draft, remove_offer_from_draft,
)
from app.services.draft_workspace import DraftWorkspaceView
from app.services.managed_creation import create_or_reconcile
from app.services.stream_publication import (
    cancel_draft, duplicate_offer_message, is_editable, publication_assignments, publish_draft, sync_campaign, workspace,
)
from app.services.stream_sync import MAX_REMOTE_ID, latest_snapshot

router = APIRouter(prefix="/internal/v1/workspace")
InputID = Annotated[int, Field(strict=True, ge=1, le=MAX_REMOTE_ID)]
RemoteID = Annotated[int, Path(ge=1, le=MAX_REMOTE_ID)]
Actor = Annotated[int, Header(alias="X-Actor-Id", ge=1, le=MAX_REMOTE_ID)]


def engine_dependency(settings: Settings = Depends(get_settings)) -> Engine:
    return get_workspace_engine(settings)


def keitaro_dependency(settings: Settings = Depends(get_settings)) -> Iterator[KeitaroAdminClient]:
    with KeitaroAdminClient.from_settings(settings) as client:
        yield client


Database = Annotated[Engine, Depends(engine_dependency)]
Tracker = Annotated[KeitaroAdminClient, Depends(keitaro_dependency)]


class CampaignInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=255)]
    alias: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")]
    geo: Annotated[str, Field(pattern=r"^[a-zA-Z]{2}$")]
    offer_id: InputID
    domain_id: InputID
    group_id: InputID
    traffic_source_id: InputID


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: Annotated[int, Field(strict=True, ge=0, lt=2**31 - 1)]


class AddInput(RevisionInput):
    offer_id: InputID


class EditInput(RevisionInput):
    action: Literal["remove", "bring_back", "pin"]
    position_id: UUID
    pinned_percent: Annotated[int, Field(strict=True, ge=0, le=100)] | None = None


class CancelInput(RevisionInput):
    confirm_uncertain: bool = False


def _summary(record: KeitaroRecord) -> dict:
    return {"id": record.id, "name": record.name}


def _draft_view(session: Session, view: DraftWorkspaceView | None) -> dict | None:
    if view is None:
        return None
    details = session.get(StreamDraft, view.draft_id).conflict_details or {}
    result = asdict(view)
    result["reason"] = details.get("reason")
    result["upstream_status"] = details.get("status_code")
    result["reconciliation_checked"] = details.get("checked") is True
    result["publication_error"] = (
        duplicate_offer_message(publication_assignments(session, view)) if view.status == DRAFT_DIRTY else None
    )
    return result


@router.get("/catalog")
def catalog(client: Tracker) -> dict:
    return {
        "domains": [_summary(record) for record in client.list_domains() if record.raw.get("state") == "active"],
        "groups": [_summary(record) for record in client.list_groups("campaigns") if record.raw.get("type") == "campaigns"],
        "sources": [_summary(record) for record in client.list_traffic_sources() if record.raw.get("state") == "active"],
    }


@router.get("/offers")
def search_offers(
    client: Tracker, q: Annotated[str, Query(max_length=255)] = "",
    ids: Annotated[str, Query(max_length=2000, pattern=r"^[0-9]+(,[0-9]+)*$")] | None = None,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> list[dict]:
    if ids is not None:
        selected = set(map(int, ids.split(",")))
        return [_summary(record) for record in client.list_offers() if record.id in selected]
    query = q.strip().casefold()
    offers = [record for record in client.list_offers() if record.raw.get("state") == "active"]
    matches = [record for record in offers if query == str(record.id) or query in record.name.casefold()]
    matches.sort(key=lambda record: (str(record.id) != query, record.name.casefold(), record.id))
    return [_summary(record) for record in matches[offset:offset + 50]]


@router.get("/campaigns/remote")
def remote_campaigns(
    client: Tracker, offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict:
    page = client.list_campaigns(offset=offset, limit=50)
    next_offset = offset + len(page) if len(page) == 50 and offset < 100_000 else None
    return {"items": [_summary(record) for record in page], "next_offset": next_offset}


@router.get("/campaigns")
def local_campaigns(engine: Database) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(select(Campaign).order_by(Campaign.name, Campaign.keitaro_campaign_id))
        return [{"id": row.keitaro_campaign_id, "name": row.name} for row in rows]


@router.post("/campaigns")
def create(payload: CampaignInput, client: Tracker, engine: Database) -> dict:
    return asdict(create_or_reconcile(engine, client, CampaignCreationRequest(**payload.model_dump())))


@router.get("/campaigns/{campaign_id}")
def campaign_workspace(campaign_id: RemoteID, engine: Database) -> dict:
    with Session(engine) as session, session.begin():
        campaign = session.scalar(select(Campaign).where(Campaign.keitaro_campaign_id == campaign_id))
        if campaign is None:
            return {"id": campaign_id, "name": None, "streams": []}
        rows = session.scalars(select(Stream).where(Stream.campaign_id == campaign.id)).all()
        draft_statuses = dict(session.execute(
            select(StreamDraft.stream_id, StreamDraft.status)
            .join(Stream, Stream.id == StreamDraft.stream_id)
            .where(Stream.campaign_id == campaign.id, StreamDraft.status.not_in(TERMINAL_DRAFT_STATUSES))
        ).all())
        streams = []
        for row in rows:
            snapshot = latest_snapshot(session, row.id)
            payload = snapshot.normalized_payload if snapshot else {}
            streams.append({
                "id": row.id, "keitaro_id": row.keitaro_stream_id, "name": row.name,
                "type": row.stream_type, "schema": row.schema_type, "position": payload.get("position"),
                "present": payload.get("present") is True, "editable": is_editable(snapshot),
                "draft_status": draft_statuses.get(row.id),
            })
        streams.sort(key=lambda row: (row["position"] is None, row["position"] or 0, row["keitaro_id"]))
        return {"id": campaign_id, "name": campaign.name, "streams": streams}


@router.post("/campaigns/{campaign_id}/fetch")
def fetch_campaign(campaign_id: RemoteID, client: Tracker, engine: Database) -> dict:
    sync_campaign(engine, client, campaign_id)
    return campaign_workspace(campaign_id, engine)


@router.post("/streams/{stream_id}/workspace")
def open_workspace(stream_id: UUID, actor_id: Actor, engine: Database) -> dict:
    with Session(engine) as session, session.begin():
        return _draft_view(session, workspace(session, stream_id, actor_id))


@router.post("/drafts/{draft_id}/offers")
def add_offer(draft_id: UUID, payload: AddInput, client: Tracker, engine: Database) -> dict:
    offer = client.get_offer(payload.offer_id)
    if offer.raw.get("state") != "active":
        raise HTTPException(status_code=422, detail="Only active Keitaro offers can be added")
    with Session(engine) as session, session.begin():
        view = add_offer_to_draft(
            session, draft_id=draft_id, expected_revision=payload.revision, keitaro_offer_id=offer.id,
        )
        return _draft_view(session, view)


@router.post("/drafts/{draft_id}/edit")
def edit(draft_id: UUID, payload: EditInput, engine: Database) -> dict:
    fields = {"draft_id": draft_id, "expected_revision": payload.revision, "position_id": payload.position_id}
    with Session(engine) as session, session.begin():
        if payload.action == "remove":
            view = remove_offer_from_draft(session, **fields)
        elif payload.action == "bring_back":
            view = bring_back_offer_in_draft(session, **fields)
        else:
            view = pin_offer_in_draft(session, **fields, pinned_percent=payload.pinned_percent)
        return _draft_view(session, view)


@router.post("/drafts/{draft_id}/cancel")
def cancel(draft_id: UUID, payload: CancelInput, actor_id: Actor, engine: Database) -> dict:
    with Session(engine) as session, session.begin():
        view = cancel_draft(session, draft_id, payload.revision, actor_id, confirm_uncertain=payload.confirm_uncertain)
        return {"draft": _draft_view(session, view)}


@router.post("/drafts/{draft_id}/publish")
def publish(draft_id: UUID, payload: RevisionInput, client: Tracker, engine: Database) -> dict:
    view = publish_draft(engine, client, draft_id, payload.revision)
    with Session(engine) as session:
        return _draft_view(session, view)
