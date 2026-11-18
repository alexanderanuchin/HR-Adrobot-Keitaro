"""Create and verify the two Keitaro flows before enabling a campaign."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from app.integrations.keitaro_admin import (
    KeitaroAdminClient,
    KeitaroError,
    KeitaroRecord,
    KeitaroResponseError,
    StreamOfferAssignment,
    stream_offer_assignments,
)
from app.services.campaign_preflight import CampaignCreationPlan, CampaignCreationRequest, prepare_campaign_creation


class CampaignCreationError(KeitaroError):
    """A failed creation step that may require reconciliation in Keitaro."""

    def __init__(self, stage: str, campaign_id: int | None, error: KeitaroError) -> None:
        self.stage = stage
        self.campaign_id = campaign_id
        super().__init__(f"Keitaro campaign creation failed during {stage}: {error}")


@dataclass(frozen=True, slots=True)
class CreatedCampaign:
    campaign: KeitaroRecord
    geo_stream: KeitaroRecord
    offer_stream: KeitaroRecord


def _country_matches(filters: object, country: str | None) -> bool:
    if country is None:
        return False
    if not isinstance(filters, list) or len(filters) != 1 or not isinstance(filters[0], dict):
        return False
    rule = filters[0]
    if rule.get("name") != "country" or rule.get("mode") != "accept":
        return False
    payload = rule.get("payload")
    if isinstance(payload, str) and payload != country:
        try:
            payload = json.loads(payload)
        except ValueError:
            return False
    return payload == country or payload == [country]


def create_campaign(client: KeitaroAdminClient, request: CampaignCreationRequest) -> CreatedCampaign:
    """Create a disabled campaign, verify both streams, then enable it.

    If a write fails, inspect Keitaro before retrying. The error provides the
    campaign ID once known. A timeout can occur after a remote write commits.
    """
    return execute_campaign_creation(client, prepare_campaign_creation(client, request))


def verify_campaign_settings(
    campaign: KeitaroRecord, plan: CampaignCreationPlan, *, state: str,
) -> None:
    fields = plan.campaign_post_fields
    if (
        campaign.name != fields["name"]
        or campaign.raw.get("state") != state
        or campaign.raw.get("alias") != fields["alias"]
        or campaign.raw.get("type") != fields["type"]
        or str(campaign.raw.get("group_id")) != fields["group_id"]
        or campaign.raw.get("traffic_source_id") != fields["traffic_source_id"]
        or campaign.raw.get("domain_id") != plan.domain_id
    ):
        raise KeitaroResponseError("Keitaro returned unexpected campaign settings")


def verify_campaign_streams(
    plan: CampaignCreationPlan, streams: tuple[KeitaroRecord, ...],
) -> tuple[KeitaroRecord, KeitaroRecord]:
    geo, fallback = plan.streams
    by_type = {
        record.raw["type"]: record for record in streams
        if isinstance(record.raw.get("type"), str)
    }
    if len(streams) != 2 or len({record.id for record in streams}) != 2 or set(by_type) != {"regular", "default"}:
        raise KeitaroResponseError("Keitaro did not return the two expected streams")
    geo_stream, offer_stream = by_type["regular"], by_type["default"]
    if (
        geo_stream.raw.get("state") != "active"
        or geo_stream.raw.get("position") != geo.order
        or geo_stream.raw.get("schema") != "redirect"
        or geo_stream.raw.get("action_type") != "http"
        or geo_stream.raw.get("action_payload") != geo.redirect_url
        or not _country_matches(geo_stream.raw.get("filters"), geo.country_code)
    ):
        raise KeitaroResponseError("Keitaro did not retain the GEO redirect settings")
    if (
        fallback.offer_id is None
        or offer_stream.raw.get("state") != "active"
        or offer_stream.raw.get("position") != fallback.order
        or offer_stream.raw.get("schema") != "landings"
        or offer_stream.raw.get("landings") != []
        or stream_offer_assignments(offer_stream) != (StreamOfferAssignment(fallback.offer_id, 100),)
    ):
        raise KeitaroResponseError("Keitaro did not retain the fallback offer settings")
    return geo_stream, offer_stream


def execute_campaign_creation(
    client: KeitaroAdminClient,
    plan: CampaignCreationPlan,
    on_created: Callable[[int], None] | None = None,
) -> CreatedCampaign:
    geo, fallback = plan.streams
    campaign_id = None
    stage = "campaign creation"
    try:
        campaign = client.create_campaign({**plan.campaign_post_fields, "state": "disabled"})
        campaign_id = campaign.id
        if on_created is not None:
            on_created(campaign_id)
        if "domain_id" not in campaign.raw:
            campaign = client.get_campaign(campaign_id)
        verify_campaign_settings(campaign, plan, state="disabled")

        stage = "GEO stream creation"
        geo_stream = client.create_stream({
            "campaign_id": campaign_id,
            "name": "GEO redirect",
            "position": geo.order,
            "type": "regular",
            "state": "active",
            "collect_clicks": True,
            "schema": "redirect",
            "action_type": "http",
            "action_options": {"url": geo.redirect_url},
            "filters": [{"name": "country", "mode": "accept", "payload": [geo.country_code]}],
        })
        stage = "offer stream creation"
        offer_stream = client.create_stream({
            "campaign_id": campaign_id,
            "name": "Fallback offer",
            "position": fallback.order,
            "type": "default",
            "state": "active",
            "collect_clicks": True,
            "schema": "landings",
            "action_type": "http",
            "landings": [],
            "offers": [{"offer_id": fallback.offer_id, "share": 100, "state": "active"}],
        })

        stage = "stream verification"
        observed_geo, observed_offer = verify_campaign_streams(plan, client.list_streams(campaign_id))
        if geo_stream.id == offer_stream.id or (observed_geo.id, observed_offer.id) != (geo_stream.id, offer_stream.id):
            raise KeitaroResponseError("Keitaro did not return the two created streams")

        stage = "campaign activation"
        campaign = client.enable_campaign(campaign_id)
        if "domain_id" not in campaign.raw:
            campaign = client.get_campaign(campaign_id)
        verify_campaign_settings(campaign, plan, state="active")
    except KeitaroError as exc:
        raise CampaignCreationError(stage, campaign_id, exc) from exc

    return CreatedCampaign(campaign, observed_geo, observed_offer)
