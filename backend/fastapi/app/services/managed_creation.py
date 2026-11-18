"""Durable campaign creation and read-only recovery of ambiguous attempts."""

from dataclasses import asdict, dataclass

from sqlalchemy import Engine

from app.db.workspace import OperationBusy, operation_session
from app.integrations.keitaro_admin import (
    KeitaroAdminClient, KeitaroError, KeitaroHTTPError, KeitaroRecord, KeitaroResponseError,
)
from app.models import CampaignOperation
from app.models.snapshot import snapshot_fingerprint
from app.services.campaign_creation import (
    CampaignCreationError, execute_campaign_creation, verify_campaign_settings, verify_campaign_streams,
)
from app.services.campaign_preflight import (
    CampaignCreationRequest, campaign_creation_plan, prepare_campaign_creation,
)
from app.services.stream_sync import record_campaign


@dataclass(frozen=True, slots=True)
class CreationResult:
    alias: str
    status: str
    campaign_id: int | None
    stage: str


def _result(operation: CampaignOperation) -> CreationResult:
    return CreationResult(operation.alias, operation.status, operation.campaign_id, operation.stage)


def _campaign_with_alias(client: KeitaroAdminClient, alias: str) -> KeitaroRecord | None:
    seen = set()
    offset = 0
    while offset < 100_000:
        page = client.list_campaigns(offset=offset, limit=200)
        if not page:
            return None
        for record in page:
            if record.id in seen:
                raise KeitaroResponseError("Keitaro repeated a campaign during pagination")
            if record.raw.get("alias") == alias:
                return record
            seen.add(record.id)
        offset += len(page)
    raise KeitaroResponseError("Keitaro campaign lookup exceeded its scan limit")


def create_or_reconcile(
    engine: Engine, client: KeitaroAdminClient, request: CampaignCreationRequest,
) -> CreationResult:
    plan = campaign_creation_plan(request)
    alias = request.alias
    # Normalize GEO before hashing so retries from different entry points
    # address the same operation.
    fingerprint = snapshot_fingerprint(asdict(plan))
    with operation_session(engine, f"create-alias:{alias}") as session:
        with session.begin():
            operation = session.get(CampaignOperation, alias)
            if operation is not None:
                if operation.request_fingerprint != fingerprint:
                    raise OperationBusy("This alias already has an operation with different input")
                if operation.status == "COMPLETED":
                    return _result(operation)
                try:
                    campaign = (
                        client.get_campaign(operation.campaign_id) if operation.campaign_id is not None
                        else _campaign_with_alias(client, alias)
                    )
                    if campaign is not None:
                        verify_campaign_settings(campaign, plan, state="active")
                        verify_campaign_streams(plan, client.list_streams(campaign.id))
                        record_campaign(session, campaign)
                        operation.status = "COMPLETED"
                        operation.campaign_id = campaign.id
                        operation.stage = "verified"
                        return _result(operation)
                except KeitaroError:
                    pass
                operation.status = "UNCERTAIN"
                return _result(operation)

            if _campaign_with_alias(client, alias) is not None:
                raise OperationBusy("A Keitaro campaign already uses this alias")
            plan = prepare_campaign_creation(client, request)
            operation = CampaignOperation(alias=alias, request_fingerprint=fingerprint,
                                          status="PENDING", stage="campaign creation")
            session.add(operation)

        def remember_campaign(campaign_id: int) -> None:
            with session.begin():
                operation.campaign_id = campaign_id
                operation.stage = "flow creation"

        try:
            created = execute_campaign_creation(client, plan, on_created=remember_campaign)
        except CampaignCreationError as exc:
            with session.begin():
                original_error = exc.__cause__
                if (
                    exc.campaign_id is None
                    and isinstance(original_error, KeitaroHTTPError)
                    and original_error.status_code in {400, 401, 402, 403, 404, 406, 422, 429}
                ):
                    # A confirmed rejection of the first POST leaves no created
                    # ID and permits a corrected attempt after the alias check.
                    session.delete(operation)
                    result = None
                else:
                    operation.status = "UNCERTAIN"
                    operation.stage = exc.stage
                    result = _result(operation)
            if result is None:
                raise original_error from None
            return result

        with session.begin():
            record_campaign(session, created.campaign)
            operation.status = "COMPLETED"
            operation.stage = "verified"
            return _result(operation)
