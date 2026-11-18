"""Validate Keitaro references and plan the two streams using read requests."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import Sequence

from app.core.config import get_settings
from app.integrations.keitaro_admin import (
    KeitaroAdminReadClient,
    KeitaroError,
    KeitaroRecord,
)


MAX_ID = 9_223_372_036_854_775_807
_ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z", re.ASCII)
_GEO = re.compile(r"[A-Z]{2}\Z", re.ASCII)


class CampaignPreflightError(ValueError):
    """Invalid input or a failed remote-reference check; no writes occurred."""


@dataclass(frozen=True, slots=True)
class CampaignCreationRequest:
    """Campaign parameters and selected Keitaro IDs."""

    name: str
    geo: str
    offer_id: int
    domain_id: int
    group_id: int
    traffic_source_id: int
    alias: str


@dataclass(frozen=True, slots=True)
class StreamIntent:
    """Describe where a stream should send traffic."""

    order: int
    kind: str
    country_code: str | None
    redirect_url: str | None
    offer_id: int | None


@dataclass(frozen=True, slots=True)
class CampaignCreationPlan:
    """Read-only verified input and the exact two-stream behavior to build."""

    campaign_post_fields: dict[str, str | int]
    domain_id: int
    streams: tuple[StreamIntent, StreamIntent]


def _positive_id(value: object, field: str) -> int:
    if type(value) is not int or not 0 < value <= MAX_ID:
        raise CampaignPreflightError(f"{field} must be a positive 64-bit integer")
    return value


def _check_request(request: CampaignCreationRequest) -> CampaignCreationRequest:
    if not isinstance(request, CampaignCreationRequest):
        raise CampaignPreflightError("Expected CampaignCreationRequest")
    name = request.name
    if (
        not isinstance(name, str)
        or name != name.strip()
        or not 1 <= len(name) <= 255
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise CampaignPreflightError("name must be 1–255 non-control characters, without outer whitespace")
    if not isinstance(request.geo, str) or any(ord(c) < 32 or ord(c) == 127 for c in request.geo):
        raise CampaignPreflightError("geo must be a two-letter country code")
    geo = request.geo.strip().upper()
    if not _GEO.fullmatch(geo):
        raise CampaignPreflightError("geo must be a two-letter ASCII country code")
    # This checks the country *format*, not ISO membership. The selected tracker
    # must validate the supported country codes when creating the actual flow.
    if not isinstance(request.alias, str) or not _ALIAS.fullmatch(request.alias):
        raise CampaignPreflightError("alias must be 3–64 lowercase ASCII slug characters")
    for attr in ("offer_id", "domain_id", "group_id", "traffic_source_id"):
        _positive_id(getattr(request, attr), attr)
    return CampaignCreationRequest(
        name=name,
        geo=geo,
        offer_id=request.offer_id,
        domain_id=request.domain_id,
        group_id=request.group_id,
        traffic_source_id=request.traffic_source_id,
        alias=request.alias,
    )


def _one_by_id(records: tuple[KeitaroRecord, ...], target_id: int, kind: str) -> KeitaroRecord:
    matches = [record for record in records if record.id == target_id]
    if len(matches) != 1:
        raise CampaignPreflightError(f"{kind} ID is missing or ambiguous in Keitaro")
    return matches[0]


def _active(record: KeitaroRecord, kind: str) -> None:
    # Fail closed: an absent state does not prove the remote entity is active.
    if record.raw.get("state") != "active":
        raise CampaignPreflightError(f"{kind} is not confirmed active in Keitaro")


def prepare_campaign_creation(
    client: KeitaroAdminReadClient,
    request: CampaignCreationRequest,
) -> CampaignCreationPlan:
    """Validate references with GET and build the planned campaign and flows."""
    values = _check_request(request)
    offer = client.get_offer(values.offer_id)
    if offer.id != values.offer_id:
        raise CampaignPreflightError("Selected offer does not match the request")
    _active(offer, "Offer")

    domain = _one_by_id(client.list_domains(), values.domain_id, "Domain")
    _active(domain, "Domain")

    group = _one_by_id(client.list_groups("campaigns"), values.group_id, "Campaign group")
    if group.raw.get("type") != "campaigns":
        raise CampaignPreflightError("Selected group is not a confirmed campaign group")

    source = _one_by_id(client.list_traffic_sources(), values.traffic_source_id, "Traffic source")
    _active(source, "Traffic source")

    return campaign_creation_plan(values)


def campaign_creation_plan(request: CampaignCreationRequest) -> CampaignCreationPlan:
    values = _check_request(request)
    return CampaignCreationPlan(
        campaign_post_fields={
            "alias": values.alias,
            "name": values.name,
            "type": "position",  # Route in order; do not split flows by weight.
            "group_id": str(values.group_id),
            "domain_id": values.domain_id,
            "traffic_source_id": values.traffic_source_id,
        },
        domain_id=values.domain_id,
        streams=(
            StreamIntent(
                order=1,
                kind="geo_redirect",
                country_code=values.geo,
                redirect_url="https://www.google.com/",
                offer_id=None,
            ),
            StreamIntent(
                order=2,
                kind="fallback_offer",
                country_code=None,
                redirect_url=None,
                offer_id=values.offer_id,
            ),
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Optional safe local probe: no raw Keitaro responses or secrets printed."""
    parser = argparse.ArgumentParser(
        description="Check campaign prerequisites using GET requests only; never create remote data"
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--geo", required=True)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--offer-id", type=int, required=True)
    parser.add_argument("--domain-id", type=int, required=True)
    parser.add_argument("--group-id", type=int, required=True)
    parser.add_argument("--traffic-source-id", type=int, required=True)
    args = parser.parse_args(argv)
    request = CampaignCreationRequest(
        name=args.name, geo=args.geo, offer_id=args.offer_id,
        domain_id=args.domain_id, group_id=args.group_id,
        traffic_source_id=args.traffic_source_id, alias=args.alias,
    )
    try:
        _check_request(request)  # Fail without opening a connection for bad inputs.
        with KeitaroAdminReadClient.from_settings(get_settings()) as client:
            plan = prepare_campaign_creation(client, request)
    except (CampaignPreflightError, KeitaroError) as exc:
        # Our own messages are sanitized; never log raw request/response bodies,
        # exception causes, URLs, headers, credentials or record names.
        print(f"PRECHECK FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("PRECHECK OK: offer, domain, campaign group and traffic source validated by GET")
    print(f"PLANNED FLOWS: {len(plan.streams)}; GEO redirect followed by fallback offer")
    print("NO WRITES: Keitaro and PostgreSQL were not changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
