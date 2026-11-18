"""Creator preflight: exact two-flow intent and read-only Keitaro validation.

No network access: httpx.MockTransport intercepts *every* creation_input.
No SQL tables, migrations, POST or tracker writes are involved.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from typing import Iterator

import httpx
import pytest

from app.integrations.keitaro_admin import KeitaroAdminReadClient
from app.services import campaign_preflight
from app.services.campaign_preflight import (
    CampaignCreationRequest,
    CampaignPreflightError,
    _check_request,
    main,
    prepare_campaign_creation,
)


ORIGIN = "https://tracker.example.invalid"
TOKEN = "mock-token-never-print"


@pytest.fixture
def creation_input() -> CampaignCreationRequest:
    return CampaignCreationRequest(
        name="Campaign AU", geo="AU", alias="campaign-au-2026",
        offer_id=4514, domain_id=1813, group_id=17, traffic_source_id=836,
    )


@contextmanager
def mock_tracker(overrides: dict[str, object] | None = None) -> Iterator[tuple[KeitaroAdminReadClient, list[httpx.Request]]]:
    samples: dict[str, object] = {
        "/admin_api/v1/offers/4514": {"id": 4514, "name": "Offer", "state": "active"},
        "/admin_api/v1/domains": [{"id": 1813, "name": "domain.example", "state": "active"}],
        "/admin_api/v1/groups": [{"id": 17, "name": "Group", "type": "campaigns"}],
        "/admin_api/v1/traffic_sources": [{"id": 836, "name": "Traffic", "state": "active"}],
    }
    if overrides:
        samples.update(overrides)
    requests: list[httpx.Request] = []

    def handle(incoming: httpx.Request) -> httpx.Response:
        requests.append(incoming)
        assert incoming.method == "GET", "Preflight must NEVER send writes"
        assert incoming.url.host == "tracker.example.invalid"
        assert incoming.headers["Api-Key"] == TOKEN
        assert TOKEN not in str(incoming.url)
        if incoming.url.path not in samples:
            raise AssertionError("Unexpected preflight route")
        return httpx.Response(200, json=samples[incoming.url.path])

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as http:
        with KeitaroAdminReadClient(base_url=ORIGIN, api_key=TOKEN, client=http) as wrapper:
            yield wrapper, requests


def test_valid_plan_has_exactly_two_position_ordered_flows(creation_input):
    with mock_tracker() as (client, sent):
        plan = prepare_campaign_creation(client, creation_input)
    assert plan.campaign_post_fields == {
        "alias": "campaign-au-2026", "name": "Campaign AU", "type": "position",
        "group_id": "17", "domain_id": 1813, "traffic_source_id": 836,
    }
    assert plan.domain_id == 1813
    assert len(plan.streams) == 2
    assert [x.order for x in plan.streams] == [1, 2]
    assert [x.kind for x in plan.streams] == ["geo_redirect", "fallback_offer"]
    assert plan.streams[0].country_code == "AU"
    assert plan.streams[0].redirect_url == "https://www.google.com/"
    assert plan.streams[0].offer_id is None
    assert plan.streams[1].country_code is None
    assert plan.streams[1].redirect_url is None
    assert plan.streams[1].offer_id == 4514
    assert [x.url.path for x in sent] == [
        "/admin_api/v1/offers/4514",
        "/admin_api/v1/domains",
        "/admin_api/v1/groups",
        "/admin_api/v1/traffic_sources",
    ]
    assert sent[2].url.query.decode() == "type=campaigns"


def test_normalizes_lowercase_geo_only(creation_input):
    validated = _check_request(replace(creation_input, geo=" au "))
    assert validated.geo == "AU"
    with mock_tracker() as (client, _):
        plan = prepare_campaign_creation(client, replace(creation_input, geo=" au "))
    assert plan.streams[0].country_code == "AU"


@pytest.mark.parametrize("geo", [None, 12, "", "A", "AUS", "A1", "中U", "🇦🇺", "AU,US", "RU US", "\nAU", "ÅU"])
def test_invalid_geo_rejected_without_http(creation_input, geo):
    with mock_tracker() as (client, sent):
        with pytest.raises(CampaignPreflightError, match="geo"):
            prepare_campaign_creation(client, replace(creation_input, geo=geo))
    assert sent == []


@pytest.mark.parametrize("alias", [None, "", "a", "ab", "CAPS", "some alias", "alias/path", "alias?x=1", "abc\n", "abc.", "аbc", "x" * 65])
def test_invalid_alias_rejected_without_http(creation_input, alias):
    with mock_tracker() as (client, sent):
        with pytest.raises(CampaignPreflightError, match="alias"):
            prepare_campaign_creation(client, replace(creation_input, alias=alias))
    assert sent == []


@pytest.mark.parametrize("name", [None, "", " ", " trailing ", "with\nnewline", "\tBad", "a" * 256, True])
def test_invalid_name_rejected_without_http(creation_input, name):
    with mock_tracker() as (client, sent):
        with pytest.raises(CampaignPreflightError, match="name"):
            prepare_campaign_creation(client, replace(creation_input, name=name))
    assert sent == []


@pytest.mark.parametrize("field", ["offer_id", "domain_id", "group_id", "traffic_source_id"])
@pytest.mark.parametrize("value", [None, 0, -1, True, 1.0, "123", 2**63])
def test_invalid_id_rejected_without_http(creation_input, field, value):
    with mock_tracker() as (client, sent):
        with pytest.raises(CampaignPreflightError, match=field):
            prepare_campaign_creation(client, replace(creation_input, **{field: value}))
    assert sent == []


def test_requires_request_dataclass_before_network():
    with mock_tracker() as (client, sent):
        with pytest.raises(CampaignPreflightError, match="CampaignCreationRequest"):
            prepare_campaign_creation(client, {"name": "Not typed"})
    assert sent == []


@pytest.mark.parametrize("path,key", [
    ("/admin_api/v1/domains", "domain"),
    ("/admin_api/v1/groups", "group"),
    ("/admin_api/v1/traffic_sources", "source"),
])
def test_missing_reference_causes_failure_not_silent_default(creation_input, path, key):
    with mock_tracker({path: []}) as (client, sent):
        with pytest.raises(CampaignPreflightError, match="missing or ambiguous"):
            prepare_campaign_creation(client, creation_input)
    assert sent and all(x.method == "GET" for x in sent)


@pytest.mark.parametrize("path", [
    "/admin_api/v1/domains", "/admin_api/v1/groups", "/admin_api/v1/traffic_sources",
])
def test_duplicate_reference_rejected(creation_input, path):
    values = {
        "/admin_api/v1/domains": {"id": 1813, "name": "Domain"},
        "/admin_api/v1/groups": {"id": 17, "name": "Group", "type": "campaigns"},
        "/admin_api/v1/traffic_sources": {"id": 836, "name": "Source"},
    }
    item = values[path]
    with mock_tracker({path: [item, item]}) as (client, _):
        with pytest.raises(CampaignPreflightError, match="ambiguous"):
            prepare_campaign_creation(client, creation_input)


@pytest.mark.parametrize("path,item", [
    ("/admin_api/v1/offers/4514", {"id": 4514, "name": "Offer", "state": "deleted"}),
    ("/admin_api/v1/domains", [{"id": 1813, "name": "Domain", "state": "deleted"}]),
    ("/admin_api/v1/traffic_sources", [{"id": 836, "name": "Source", "state": "disabled"}]),
])
def test_explicitly_inactive_records_are_rejected(creation_input, path, item):
    with mock_tracker({path: item}) as (client, sent):
        with pytest.raises(CampaignPreflightError, match="not confirmed active"):
            prepare_campaign_creation(client, creation_input)
    assert sent and all(x.method == "GET" for x in sent)


def test_foreign_group_type_rejected_even_when_id_matches(creation_input):
    with mock_tracker({"/admin_api/v1/groups": [{"id": 17, "name": "Offer Group", "type": "offers"}]}) as (client, _):
        with pytest.raises(CampaignPreflightError, match="not a confirmed campaign group"):
            prepare_campaign_creation(client, creation_input)


@pytest.mark.parametrize("path, replacement", [
    ("/admin_api/v1/offers/4514", {"id": 4514, "name": "Offer"}),
    ("/admin_api/v1/domains", [{"id": 1813, "name": "Domain"}]),
    ("/admin_api/v1/traffic_sources", [{"id": 836, "name": "Source"}]),
])
def test_missing_state_fails_closed(creation_input, path, replacement):
    with mock_tracker({path: replacement}) as (client, _):
        with pytest.raises(CampaignPreflightError, match="not confirmed active"):
            prepare_campaign_creation(client, creation_input)


def test_missing_group_type_fails_closed(creation_input):
    with mock_tracker({"/admin_api/v1/groups": [{"id": 17, "name": "Group"}]}) as (client, _):
        with pytest.raises(CampaignPreflightError, match="not a confirmed campaign group"):
            prepare_campaign_creation(client, creation_input)


def test_injected_response_cannot_leak_secrets_to_repr_or_payload(creation_input):
    secret = "remote-sensitive-internal-value"
    with mock_tracker({"/admin_api/v1/offers/4514": {
        "id": 4514, "name": "Offer", "state": "active", "metadata": secret,
    }}) as (client, _):
        plan = prepare_campaign_creation(client, creation_input)
    assert secret not in repr(plan)
    assert TOKEN not in repr(plan)
    assert "metadata" not in plan.campaign_post_fields


def test_cli_sanitized_success_uses_get_only(monkeypatch, capsys):
    class ClientStub:
        @classmethod
        @contextmanager
        def from_settings(cls, settings):
            with mock_tracker() as (client, _):
                yield client

    # from_settings must hand the CLI a usable context manager wrapper.
    monkeypatch.setattr(campaign_preflight, "KeitaroAdminReadClient", ClientStub)
    monkeypatch.setattr(campaign_preflight, "get_settings", lambda: object())
    args = [
        "--name", "Campaign AU", "--geo", "AU", "--alias", "campaign-au-2026",
        "--offer-id", "4514", "--domain-id", "1813", "--group-id", "17",
        "--traffic-source-id", "836",
    ]
    assert main(args) == 0
    out = capsys.readouterr()
    assert "PRECHECK OK" in out.out
    assert "NO WRITES" in out.out
    assert TOKEN not in out.out
    assert "domain.example" not in out.out
    assert "Campaign AU" not in out.out
    assert out.err == ""


def test_cli_fails_before_connecting_on_bad_geo(monkeypatch, capsys):
    monkeypatch.setattr(campaign_preflight, "get_settings", lambda: (_ for _ in ()).throw(AssertionError("no network")))
    args = [
        "--name", "Campaign AU", "--geo", "AU,US", "--alias", "campaign-au-2026",
        "--offer-id", "4514", "--domain-id", "1813", "--group-id", "17",
        "--traffic-source-id", "836",
    ]
    assert main(args) == 1
    captured = capsys.readouterr()
    assert "PRECHECK FAILED" in captured.err
    assert captured.out == ""
