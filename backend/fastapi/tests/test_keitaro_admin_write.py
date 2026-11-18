import json
from contextlib import ExitStack

import httpx
import pytest

from app.integrations.keitaro_admin import (
    KeitaroAdminClient,
    KeitaroRequestError,
    KeitaroResponseError,
    KeitaroTimeoutError,
    KeitaroValidationError,
    StreamOfferAssignment,
)
from app.services.campaign_creation import CampaignCreationError, create_campaign
from app.services.campaign_preflight import CampaignCreationRequest


@pytest.fixture
def tracker():
    requests = []

    def open_client(handler):
        def transport(request):
            requests.append(request)
            assert request.url.host == "tracker.example.invalid"
            assert request.headers["Api-Key"] == "mock-key"
            assert request.headers["Accept"] == "application/json"
            if request.content:
                assert request.headers["Content-Type"] == "application/json"
            return handler(request)

        return KeitaroAdminClient(
            base_url="https://tracker.example.invalid",
            api_key="mock-key",
            client=clients.enter_context(httpx.Client(transport=httpx.MockTransport(transport))),
        )

    with ExitStack() as clients:
        yield open_client, requests


def test_replace_offer_associations_supports_removal_reweighting_and_disabled_entries(tracker):
    open_client, requests = tracker
    expected_requests = [
        {"offer_id": 13, "share": 0, "state": "disabled"},
        {"offer_id": 14, "share": 100, "state": "active"},
    ]

    def handle(request):
        assert json.loads(request.content) == {"offers": expected_requests}
        assert not request.url.query
        return httpx.Response(200, json={
            "id": 5, "name": "Offer flow", "campaign_id": 6, "schema": "landings",
            "offers": [{**offer, "id": index, "stream_id": 5} for index, offer in enumerate(expected_requests, 1)],
            "filters": [{"name": "country", "mode": "accept", "payload": ["DE"]}],
        })

    result = open_client(handle).replace_stream_offers(5, [
        StreamOfferAssignment(13, 0, "disabled"), StreamOfferAssignment(14, 100),
    ])
    assert result.raw["filters"][0]["payload"] == ["DE"]
    assert [(r.method, r.url.path) for r in requests] == [("PUT", "/admin_api/v1/streams/5")]


def test_replace_offers_sends_explicit_empty_array(tracker):
    open_client, requests = tracker
    client = open_client(lambda _: httpx.Response(200, json={"id": 5, "name": "Flow", "offers": []}))
    client.replace_stream_offers(5, [])
    assert json.loads(requests[0].content) == {"offers": []}


@pytest.mark.parametrize("assignment", [
    StreamOfferAssignment(True, 10), StreamOfferAssignment(0, 10),
    StreamOfferAssignment(14, -1), StreamOfferAssignment(14, 101),
    StreamOfferAssignment(14, True), StreamOfferAssignment(14, 2.5),
    StreamOfferAssignment(14, 100, "deleted"),
])
def test_invalid_offer_assignment_is_rejected_before_write(tracker, assignment):
    open_client, requests = tracker
    client = open_client(lambda _: pytest.fail("Unexpected HTTP request"))
    with pytest.raises(KeitaroRequestError):
        client.replace_stream_offers(5, [assignment])
    assert requests == []


@pytest.mark.parametrize("response", [
    {"id": 6, "name": "Other flow", "offers": []},
    {"id": 5, "name": "Flow", "offers": []},
    {"id": 5, "name": "Flow", "offers": [{"offer_id": 14, "share": True, "state": "active"}]},
    {"id": 5, "name": "Flow", "offers": [{"offer_id": 14, "share": 99, "state": "active"}]},
])
def test_wrong_stream_or_assignment_response_is_rejected(tracker, response):
    open_client, requests = tracker
    client = open_client(lambda _: httpx.Response(200, json=response))
    with pytest.raises(KeitaroResponseError):
        client.replace_stream_offers(5, [StreamOfferAssignment(14, 100)])
    assert len(requests) == 1


def test_create_stream_rejects_wrong_campaign(tracker):
    open_client, requests = tracker
    client = open_client(lambda _: httpx.Response(200, json={"id": 5, "name": "Flow", "campaign_id": 7}))
    with pytest.raises(KeitaroResponseError, match="different campaign"):
        client.create_stream({"campaign_id": 6})
    assert len(requests) == 1


def test_validation_error_hides_body_and_does_not_retry(tracker):
    open_client, requests = tracker
    client = open_client(lambda _: httpx.Response(406, json={"error": "private payload"}))
    with pytest.raises(KeitaroValidationError) as exc:
        client.create_campaign({"alias": "demo", "name": "Demo"})
    assert exc.value.status_code == 406
    assert "private payload" not in str(exc.value)
    assert len(requests) == 1


def test_timed_out_write_is_not_retried(tracker):
    open_client, requests = tracker

    def handle(request):
        raise httpx.ReadTimeout("private exception", request=request)

    with pytest.raises(KeitaroTimeoutError, match="POST timed out") as exc:
        open_client(handle).create_campaign({"alias": "demo", "name": "Demo"})
    assert "private exception" not in str(exc.value)
    assert len(requests) == 1


@pytest.mark.parametrize("failure", [None, "domain mismatch", "second stream", "readback", "activation"])
def test_create_campaign_verifies_two_flows_before_activation(tracker, failure):
    open_client, requests = tracker
    references = {
        "/offers/11": {"id": 11, "name": "Offer", "state": "active"},
        "/domains": [{"id": 12, "name": "domain.example", "state": "active"}],
        "/groups": [{"id": 13, "name": "Group", "type": "campaigns"}],
        "/traffic_sources": [{"id": 14, "name": "Traffic source", "state": "active"}],
    }
    campaign = {
        "id": 21, "alias": "demo-au", "name": "Demo", "group_id": 13,
        "domain_id": 12, "traffic_source_id": 14, "type": "position", "state": "disabled",
    }
    geo_fields = {
        "campaign_id": 21, "name": "GEO redirect", "type": "regular", "position": 1,
        "state": "active", "collect_clicks": True, "schema": "redirect", "action_type": "http",
        "action_payload": "https://www.google.com/",
        "filters": [{"name": "country", "mode": "accept", "payload": ["AU"]}],
    }
    offer_fields = {
        "campaign_id": 21, "name": "Fallback offer", "type": "default", "position": 2,
        "state": "active", "collect_clicks": True, "schema": "landings", "action_type": "http",
        "landings": [], "offers": [{"offer_id": 11, "state": "active", "share": 100}],
    }
    geo_flow = {
        "id": 22, "campaign_id": 21, "name": "GEO redirect", "type": "regular", "position": 1,
        "state": "active", "collect_clicks": True, "schema": "redirect", "action_type": "http",
        "action_payload": "https://www.google.com/",
        "filters": [{"id": 31, "stream_id": 22, "name": "country", "mode": "accept", "payload": '["AU"]'}],
    }
    offer_flow = {**offer_fields, "id": 23, "offers": [{**offer_fields["offers"][0], "id": 32, "stream_id": 23}]}
    activated = False

    def handle(request):
        nonlocal activated
        path = request.url.path.removeprefix("/admin_api/v1")
        if path in references:
            assert request.method == "GET"
            assert request.url.params == httpx.QueryParams({"type": "campaigns"} if path == "/groups" else {})
            return httpx.Response(200, json=references[path])
        assert not request.url.query
        if path == "/campaigns":
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "alias": "demo-au", "name": "Demo", "group_id": "13", "domain_id": 12,
                "traffic_source_id": 14, "type": "position", "state": "disabled",
            }
            return httpx.Response(200, json={key: value for key, value in campaign.items() if key != "domain_id"})
        if path == "/campaigns/21":
            assert request.method == "GET"
            return httpx.Response(200, json={
                **campaign, "domain_id": 99 if failure == "domain mismatch" else 12,
                "state": "active" if activated else "disabled",
            })
        if path == "/streams":
            assert request.method == "POST"
            fields = json.loads(request.content)
            if fields["position"] == 1:
                assert fields == geo_fields
                return httpx.Response(200, json=geo_flow)
            assert fields == offer_fields
            if failure == "second stream":
                return httpx.Response(406, json={"error": "private failure"})
            return httpx.Response(200, json=offer_flow)
        if path == "/campaigns/21/streams":
            assert request.method == "GET"
            if failure == "readback":
                return httpx.Response(200, json=[{
                    **geo_flow, "action_payload": None, "action_options": {"url": "https://www.google.com/"},
                }, offer_flow])
            return httpx.Response(200, json=[offer_flow, geo_flow])
        if path == "/campaigns/21/enable":
            assert request.method == "POST"
            assert not request.content
            if failure == "activation":
                raise httpx.ReadTimeout("private failure", request=request)
            activated = True
            return httpx.Response(200)
        pytest.fail(f"Unexpected path {path}")

    request = CampaignCreationRequest(name="Demo", alias="demo-au", geo="au", offer_id=11,
                                      domain_id=12, group_id=13, traffic_source_id=14)
    client = open_client(handle)
    expected_writes = ["/campaigns"] if failure == "domain mismatch" else ["/campaigns", "/streams", "/streams"]
    if failure is None:
        result = create_campaign(client, request)
        assert (result.campaign.id, result.geo_stream.id, result.offer_stream.id) == (21, 22, 23)
        assert result.campaign.raw["state"] == "active"
    else:
        stages = {"domain mismatch": "campaign creation",
                  "second stream": "offer stream creation", "readback": "stream verification",
                  "activation": "campaign activation"}
        with pytest.raises(CampaignCreationError) as exc:
            create_campaign(client, request)
        assert exc.value.campaign_id == 21
        assert exc.value.stage == stages[failure]
        assert "private failure" not in str(exc.value)
    if failure in (None, "activation"):
        expected_writes.append("/campaigns/21/enable")
        assert requests[-3 if failure is None else -2].url.path == "/admin_api/v1/campaigns/21/streams"
        if failure is None:
            assert (requests[-1].method, requests[-1].url.path) == ("GET", "/admin_api/v1/campaigns/21")
    assert [r.url.path.removeprefix("/admin_api/v1") for r in requests if r.method != "GET"] == expected_writes
