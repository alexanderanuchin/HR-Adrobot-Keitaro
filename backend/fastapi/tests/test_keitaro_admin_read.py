"""Read-only Keitaro Admin API contract tests; no real tracker is contacted.

Every outbound request is intercepted by httpx.MockTransport, and the API key
is a fabricated test value. These are NOT live Keitaro integration tests.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import httpx
import pytest

from app.integrations.keitaro_admin import (
    KeitaroAdminReadClient,
    KeitaroAuthenticationError,
    KeitaroConfigurationError,
    KeitaroEditionError,
    KeitaroHTTPError,
    KeitaroNotFoundError,
    KeitaroRateLimitError,
    KeitaroRequestError,
    KeitaroResponseError,
    KeitaroTimeoutError,
    KeitaroTransportError,
    KeitaroUpstreamError,
    KeitaroValidationError,
)


ROOT = "https://tracker.example.invalid"
TOKEN = "fake-read-only-api-key-DO-NOT-USE"


@contextmanager
def mock_client(
    callback: Callable[[httpx.Request], httpx.Response] | None = None,
) -> Iterator[tuple[KeitaroAdminReadClient, list[httpx.Request]]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if callback is not None:
            return callback(request)
        return httpx.Response(200, json=[{"id": 7, "name": "Example"}])

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        wrapper = KeitaroAdminReadClient(base_url=ROOT + "/", api_key=TOKEN, client=http_client)
        with wrapper:
            yield wrapper, requests
        # The user-owned HTTPX client remains usable after wrapper context.
        assert not http_client.is_closed


@pytest.mark.parametrize(
    "action,path,params",
    [
        (lambda c: c.list_campaigns(), "/admin_api/v1/campaigns", ""),
        (lambda c: c.list_campaigns(offset=5, limit=20), "/admin_api/v1/campaigns", "offset=5&limit=20"),
        (lambda c: c.list_campaigns(limit=1001), "/admin_api/v1/campaigns", "limit=1001"),
        (lambda c: c.list_offers(), "/admin_api/v1/offers", ""),
        (lambda c: c.list_domains(), "/admin_api/v1/domains", ""),
        (lambda c: c.list_groups(), "/admin_api/v1/groups", "type=campaigns"),
        (lambda c: c.list_groups("offers"), "/admin_api/v1/groups", "type=offers"),
        (lambda c: c.list_traffic_sources(), "/admin_api/v1/traffic_sources", ""),
    ],
)
def test_list_routes_are_get_only_and_never_send_key_as_query(action, path, params):
    with mock_client() as (client, requests):
        records = action(client)
    assert [(r.id, r.name) for r in records] == [(7, "Example")]
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == path
    assert request.url.query.decode() == params
    assert request.headers["Api-Key"] == TOKEN
    assert request.headers["Accept"] == "application/json"
    assert TOKEN not in str(request.url)
    assert not request.content


@pytest.mark.parametrize(
    "action,path",
    [
        (lambda c: c.get_campaign(17), "/admin_api/v1/campaigns/17"),
        (lambda c: c.get_offer(17), "/admin_api/v1/offers/17"),
        (lambda c: c.get_stream(17), "/admin_api/v1/streams/17"),
    ],
)
def test_get_details_and_exact_id_validation(action, path):
    with mock_client(lambda request: httpx.Response(200, json={"id": 17, "name": "Known", "custom": {"ok": True}})) as (client, requests):
        result = action(client)
    assert result.id == 17
    assert result.raw["custom"] == {"ok": True}
    assert requests[0].url.path == path


@pytest.mark.parametrize("action", ["get_campaign", "get_offer", "get_stream"])
def test_detail_id_mismatch_is_not_accepted(action):
    with mock_client(lambda _: httpx.Response(200, json={"id": 999, "name": "Wrong"})) as (client, _):
        with pytest.raises(KeitaroResponseError, match="wrong"):
            getattr(client, action)(17)


def test_streams_use_campaign_scoped_path_and_preserve_raw_offers_filters_and_redirect():
    payload = [
        {"id": 11, "name": "Country", "campaign_id": 17, "schema": "redirect", "filters": [{"name": "country", "payload": "AU"}], "action_payload": "https://www.google.com/", "offers": []},
        {"id": 12, "name": "Offers", "campaign_id": 17, "schema": "offers", "filters": [], "offers": [{"id": 2, "stream_id": 12, "offer_id": 4514, "share": 33}]},
    ]
    with mock_client(lambda _: httpx.Response(200, json=payload)) as (client, reqs):
        result = client.list_streams(17)
    assert reqs[0].url.path == "/admin_api/v1/campaigns/17/streams"
    assert [r.id for r in result] == [11, 12]
    assert result[0].raw["filters"] == payload[0]["filters"]
    assert result[1].raw["offers"] == payload[1]["offers"]
    assert result[1].raw["offers"][0]["share"] == 33


def test_streams_from_another_campaign_are_rejected():
    with mock_client(lambda _: httpx.Response(200, json=[{"id": 9, "name": "Foreign", "campaign_id": 18}])) as (client, _):
        with pytest.raises(KeitaroResponseError, match="different campaign"):
            client.list_streams(17)


def test_streams_missing_campaign_owner_are_rejected():
    with mock_client(lambda _: httpx.Response(200, json=[{"id": 9, "name": "NoOwner"}])) as (client, _):
        with pytest.raises(KeitaroResponseError, match="different campaign"):
            client.list_streams(17)


@pytest.mark.parametrize("status,error", [
    (400, KeitaroValidationError),
    (401, KeitaroAuthenticationError),
    (402, KeitaroEditionError),
    (403, KeitaroAuthenticationError),
    (404, KeitaroNotFoundError),
    (406, KeitaroValidationError),
    (422, KeitaroValidationError),
    (429, KeitaroRateLimitError),
    (500, KeitaroUpstreamError),
    (503, KeitaroUpstreamError),
    (302, KeitaroHTTPError),
])
def test_http_status_error_does_not_leak_body_or_key(status, error):
    secret_response = "sensitive-body-NEVER-LOG"
    with mock_client(lambda _: httpx.Response(status, text=secret_response, headers={"Location": "https://elsewhere.example"})) as (client, reqs):
        with pytest.raises(error) as err:
            client.list_offers()
    assert err.value.status_code == status
    assert TOKEN not in str(err.value)
    assert secret_response not in str(err.value)
    assert len(reqs) == 1


@pytest.mark.parametrize("payload", [
    {"data": []}, None, "abc", 3, [12], [{"name": "missing ID"}],
    [{"id": True, "name": "booleans are not IDs"}],
    [{"id": 0, "name": "bad"}], [{"id": 1, "name": 8}],
])
def test_malformed_collection_is_rejected(payload):
    with mock_client(lambda _: httpx.Response(200, json=payload)) as (client, _):
        with pytest.raises(KeitaroResponseError):
            client.list_offers()


def test_empty_list_is_valid():
    with mock_client(lambda _: httpx.Response(200, json=[])) as (client, _):
        assert client.list_offers() == ()


def test_invalid_json_is_sanitized():
    with mock_client(lambda _: httpx.Response(200, content=b"not-json-PRIVATE")) as (client, _):
        with pytest.raises(KeitaroResponseError) as err:
            client.list_offers()
    assert "PRIVATE" not in str(err.value)


def test_unsupported_redirect_is_not_followed_and_key_not_sent_to_other_origin():
    with mock_client(lambda _: httpx.Response(302, headers={"Location": "https://other.invalid/collect"})) as (client, reqs):
        with pytest.raises(KeitaroHTTPError) as err:
            client.list_offers()
    assert err.value.status_code == 302
    assert len(reqs) == 1
    assert reqs[0].url.host == "tracker.example.invalid"


def test_timeout_is_sanitized_and_no_retry():
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"network failed and {TOKEN} key", request=request)
    with mock_client(fail) as (client, reqs):
        with pytest.raises(KeitaroTimeoutError) as err:
            client.list_offers()
    assert TOKEN not in str(err.value)
    assert len(reqs) == 1


def test_transport_error_is_sanitized_and_no_retry():
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"tls failed with {TOKEN}", request=request)
    with mock_client(fail) as (client, reqs):
        with pytest.raises(KeitaroTransportError) as err:
            client.list_offers()
    assert TOKEN not in str(err.value)
    assert len(reqs) == 1


@pytest.mark.parametrize("bad_url", [
    "http://tracker.example.invalid", "https://user:password@tracker.example.invalid",
    "https://tracker.example.invalid/admin", "https://tracker.example.invalid/?key=1",
    "https://tracker.example.invalid/#fragment", "https://tracker.example.invalid:garbage",
    "https://tracker.example.invalid ", "", "ftp://tracker.example.invalid",
])
def test_invalid_tracker_origin_rejected_before_http_client_creation(bad_url):
    with pytest.raises(KeitaroConfigurationError):
        KeitaroAdminReadClient(base_url=bad_url, api_key=TOKEN)


@pytest.mark.parametrize("bad_key", ["", " ", " secret ", "line\nbreak", "line\rbreak", "nul\x00key", "токен"])
def test_invalid_key_rejected_before_request(bad_key):
    with pytest.raises(KeitaroConfigurationError):
        KeitaroAdminReadClient(base_url=ROOT, api_key=bad_key)


@pytest.mark.parametrize("bad_timeout", [0, -1, 121, float("nan"), float("inf"), True, "15"])
def test_invalid_timeout_rejected(bad_timeout):
    with pytest.raises(KeitaroConfigurationError):
        KeitaroAdminReadClient(base_url=ROOT, api_key=TOKEN, timeout=bad_timeout)


@pytest.mark.parametrize("bad_id", [0, -1, True, "123", 1.3, 2**63])
def test_bad_ids_rejected_before_request(bad_id):
    with mock_client() as (client, reqs):
        for action in [client.get_offer, client.get_campaign, client.get_stream, client.list_streams]:
            with pytest.raises(KeitaroRequestError):
                action(bad_id)
    assert reqs == []


@pytest.mark.parametrize("offset,limit", [(-1, None), (True, None), ("1", None), (0, 0), (0, -1), (0, 1.5), (0, True)])
def test_bad_pagination_rejected_without_request(offset, limit):
    with mock_client() as (client, reqs):
        with pytest.raises(KeitaroRequestError):
            client.list_campaigns(offset=offset, limit=limit)
    assert reqs == []


@pytest.mark.parametrize("group_type", ["not-a-group", "", 2, None])
def test_bad_group_type_rejected_without_request(group_type):
    with mock_client() as (client, reqs):
        with pytest.raises(KeitaroRequestError):
            client.list_groups(group_type)
    assert reqs == []


def test_repr_does_not_reveal_full_payload():
    with mock_client(lambda _: httpx.Response(200, json=[{"id": 8, "name": "Entity", "api_key": "hidden-data"}])) as (client, _):
        record = client.list_offers()[0]
    assert record.raw["api_key"] == "hidden-data"
    assert "hidden-data" not in repr(record)
    assert TOKEN not in repr(client)


def test_owned_client_can_close_and_injected_client_stays_open():
    owned = KeitaroAdminReadClient(base_url=ROOT, api_key=TOKEN)
    assert not owned._client.is_closed
    owned.close()
    assert owned._client.is_closed
    with mock_client() as (external, _):
        external.close()
        assert not external._client.is_closed


def test_from_settings_uses_server_side_configuration_and_timeout():
    from app.core.config import Settings

    settings = Settings(
        keitaro_base_url=ROOT,
        keitaro_api_key=TOKEN,
        keitaro_http_timeout=3.75,
    )
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        wrapper = KeitaroAdminReadClient.from_settings(settings, client=transport)
        assert wrapper.list_offers() == ()
        assert wrapper._timeout == 3.75
    assert observed[0].headers["Api-Key"] == TOKEN
    assert observed[0].url.host == "tracker.example.invalid"


def test_redirects_blocked_even_if_injected_httpx_client_normally_follows():
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(301, headers={"Location": "https://attacker.invalid/collect"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as transport:
        wrapper = KeitaroAdminReadClient(base_url=ROOT, api_key=TOKEN, client=transport)
        with pytest.raises(KeitaroHTTPError) as err:
            wrapper.list_campaigns()
    assert err.value.status_code == 301
    assert len(observed) == 1
