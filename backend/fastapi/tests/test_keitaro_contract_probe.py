"""A-01..A-04: confidential, opt-in GET-only Keitaro contract inspection.

Every request is caught by MockTransport. No database, live account, network
connection, API writes or real credentials are used by these tests.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator

import httpx
import pytest

from app.integrations import keitaro_contract_probe as probe
from app.integrations.keitaro_admin import KeitaroAdminReadClient

ORIGIN = "https://tracker.example.invalid"
TOKEN = "MOCK-SECRET-DO-NOT-LOG"
PRIVATE = "sensitive-name-or-key-DO-NOT-LOG"
BASE = "/admin_api/v1"


@contextmanager
def mocked_tracker(replies: dict[str, object] | None = None) -> Iterator[tuple[KeitaroAdminReadClient, list[httpx.Request]]]:
    samples = {
        "/campaigns": [{"id": 3, "name": PRIVATE, "state": "active", "alias": PRIVATE}],
        "/offers": [{"id": 5, "name": PRIVATE, "state": "active", "action_payload": PRIVATE}],
        "/domains": [{"id": 7, "name": PRIVATE, "state": "active", "url": PRIVATE}],
        "/groups": [{"id": 9, "name": PRIVATE, "type": "campaigns", "custom_secret": PRIVATE}],
        "/traffic_sources": [{"id": 11, "name": PRIVATE, "state": "active"}],
        "/offers/5": {"id": 5, "name": PRIVATE, "state": "active", "other": PRIVATE},
        "/campaigns/3": {"id": 3, "name": PRIVATE, "state": "active", "group_id": 9},
        "/campaigns/3/streams": [
            {"id": 21, "name": PRIVATE, "campaign_id": 3, "schema": "offers", "type": "regular",
             "offers": [{"id": 31, "offer_id": 5, "share": 66, "password": PRIVATE}],
             "filters": [{"name": PRIVATE, "payload": PRIVATE}]},
        ],
        "/streams/21": {"id": 21, "name": PRIVATE, "campaign_id": 3, "schema": "offers",
                        "offers": [{"offer_id": 5, "share": 66}], "action_payload": PRIVATE},
    }
    if replies:
        samples.update(replies)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET", "probe must not write"
        assert request.url.host == "tracker.example.invalid"
        assert request.headers.get("Api-Key") == TOKEN
        route = request.url.path.removeprefix(BASE)
        value = samples.get(route)
        assert value is not None, f"Unexpected mocked route: {route}"
        if isinstance(value, httpx.Response):
            return value
        if isinstance(value, BaseException):
            raise value
        return httpx.Response(200, json=value)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with KeitaroAdminReadClient(base_url=ORIGIN, api_key=TOKEN, client=transport) as client:
            yield client, seen


def _assert_clean(report: object) -> None:
    serialized = json.dumps(report)
    for secret in (TOKEN, PRIVATE, ORIGIN, "/admin_api/v1", '"id": 21', "password", "custom_secret"):
        assert secret not in serialized


def test_default_reads_exactly_five_catalogs_without_remote_mutation():
    with mocked_tracker() as (client, requests):
        report = probe.probe_keitaro_contract(client)
    assert report["ok"] is True
    assert report["writes_performed"] is False
    assert list(report["checks"]) == [
        "campaigns", "offers", "domains", "campaign_groups", "traffic_sources",
    ]
    assert [r.url.path for r in requests] == [
        f"{BASE}/campaigns", f"{BASE}/offers", f"{BASE}/domains",
        f"{BASE}/groups", f"{BASE}/traffic_sources",
    ]
    assert requests[0].url.query.decode() == "limit=1"
    assert requests[3].url.query.decode() == "type=campaigns"
    assert all(r.method == "GET" for r in requests)
    assert report["checks"]["offers"]["sample"]["known_enums"] == {"state": "active"}
    _assert_clean(report)


def test_optional_ids_probe_explicit_campaign_streams_and_offer_detail():
    with mocked_tracker() as (client, requests):
        report = probe.probe_keitaro_contract(client, campaign_id=3, offer_id=5)
    assert report["ok"] is True
    assert len(requests) == 9
    assert [r.url.path for r in requests][-4:] == [
        f"{BASE}/offers/5", f"{BASE}/campaigns/3", f"{BASE}/campaigns/3/streams", f"{BASE}/streams/21",
    ]
    assert report["checks"]["stream_detail"]["sample"]["fields"]["offers"] == "list"
    stream = report["checks"]["campaign_streams"]["sample"]
    assert stream["known_enums"]["schema"] == "offers"
    assert stream["fields"]["campaign_id"] == "int"
    assert stream["offers_item_fields"] == {"id": "int", "offer_id": "int", "share": "int"}
    assert stream["filters_item_fields"] == {"name": "str"}
    _assert_clean(report)


@pytest.mark.parametrize("value", [True, False, -1, 0, "3", 1.25, 2**63])
@pytest.mark.parametrize("parameter", ["campaign_id", "offer_id"])
def test_bad_ids_fail_before_first_request(parameter, value):
    with mocked_tracker() as (client, requests):
        with pytest.raises(ValueError, match="positive"):
            probe.probe_keitaro_contract(client, **{parameter: value})
    assert requests == []


def test_catalog_empty_is_not_an_error_or_fabricated_entity():
    with mocked_tracker({"/offers": []}) as (client, _):
        result = probe.probe_keitaro_contract(client)
    assert result["ok"] is True
    assert result["checks"]["offers"] == {"status": "empty"}
    _assert_clean(result)


def test_record_unknown_state_value_never_echoed():
    with mocked_tracker({"/offers": [{"id": 5, "name": PRIVATE, "state": PRIVATE}]}) as (client, _):
        result = probe.probe_keitaro_contract(client)
    assert result["checks"]["offers"]["sample"]["known_enums"] == {"state": "other"}
    _assert_clean(result)


@pytest.mark.parametrize("status", [301, 401, 402, 403, 404, 429, 500, 503])
def test_remote_errors_are_sanitized_and_other_catalogs_still_checked(status):
    with mocked_tracker({"/offers": httpx.Response(status, text=PRIVATE)}) as (client, requests):
        report = probe.probe_keitaro_contract(client)
    assert len(requests) == 5
    assert report["ok"] is False
    assert report["checks"]["offers"] == {"status": "error", "error": "http", "http_status": status}
    assert report["checks"]["domains"]["status"] == "ok"
    _assert_clean(report)


def test_stream_owner_mismatch_fails_closed_without_leaking_data():
    payload = [{"id": 21, "name": PRIVATE, "campaign_id": 500}]
    with mocked_tracker({"/campaigns/3/streams": payload}) as (client, requests):
        report = probe.probe_keitaro_contract(client, campaign_id=3)
    assert len(requests) == 7
    assert report["ok"] is False
    assert report["checks"]["campaign_streams"] == {
        "status": "error", "error": "KeitaroResponseError"
    }
    _assert_clean(report)


def test_missing_stream_campaign_id_reports_shape_error_not_false_success():
    with mocked_tracker({"/campaigns/3/streams": [{"id": 21, "name": PRIVATE}]}) as (client, _):
        result = probe.probe_keitaro_contract(client, campaign_id=3)
    assert result["checks"]["campaign_streams"]["error"] == "KeitaroResponseError"


def test_transport_error_never_leaks_credentials_or_raised_message():
    with mocked_tracker({"/domains": httpx.ConnectError(f"{TOKEN} {PRIVATE}")}) as (client, _):
        result = probe.probe_keitaro_contract(client)
    assert result["ok"] is False
    assert result["checks"]["domains"] == {"status": "error", "error": "KeitaroTransportError"}
    _assert_clean(result)


def test_timeout_never_retries():
    with mocked_tracker({"/offers": httpx.ReadTimeout(PRIVATE)}) as (client, requests):
        result = probe.probe_keitaro_contract(client)
    assert len([r for r in requests if r.url.path == f"{BASE}/offers"]) == 1
    assert result["checks"]["offers"]["error"] == "KeitaroTimeoutError"
    _assert_clean(result)


def test_malformed_json_returns_safe_shape_error():
    with mocked_tracker({"/domains": httpx.Response(200, content=PRIVATE.encode())}) as (client, _):
        result = probe.probe_keitaro_contract(client)
    assert result["checks"]["domains"]["error"] == "KeitaroResponseError"
    _assert_clean(result)


def test_nested_untrusted_values_and_unknown_field_names_do_not_leave_probe():
    payload = [{
        "id": 5, "name": PRIVATE, "state": "active", "secret_access_token": PRIVATE,
        "offers": [{"offer_id": 7, "name": PRIVATE, "secret_password": PRIVATE}],
        "action_payload": {"secret": PRIVATE}, "filters": [PRIVATE],
    }]
    with mocked_tracker({"/offers": payload}) as (client, _):
        report = probe.probe_keitaro_contract(client)
    fields = report["checks"]["offers"]["sample"]
    assert fields["fields"]["action_payload"] == "object"
    assert fields["filters_item_type"] == "str"
    assert fields["offers_item_fields"] == {"name": "str", "offer_id": "int"}
    assert "secret_password" not in json.dumps(report)
    assert "secret_access_token" not in json.dumps(report)
    _assert_clean(report)


def test_cli_success_prints_only_safe_json(monkeypatch, capsys):
    class Factory:
        @classmethod
        @contextmanager
        def from_settings(cls, settings):
            with mocked_tracker() as (client, _):
                yield client

    monkeypatch.setattr(probe, "KeitaroAdminReadClient", Factory)
    monkeypatch.setattr(probe, "get_settings", lambda: object())
    assert probe.main(["--campaign-id", "3", "--offer-id", "5"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["writes_performed"] is False
    _assert_clean(output.out)


def test_cli_bad_configuration_is_sanitized(monkeypatch, capsys):
    monkeypatch.setattr(probe, "get_settings", lambda: (_ for _ in ()).throw(ValueError(PRIVATE)))
    assert probe.main([]) == 2
    out = capsys.readouterr()
    assert json.loads(out.out)["error"] == "invalid_configuration_or_arguments"
    assert PRIVATE not in out.out
    assert out.err == ""


def test_cli_bad_campaign_id_returns_setup_error_before_client_creation(monkeypatch, capsys):
    monkeypatch.setattr(probe, "get_settings", lambda: (_ for _ in ()).throw(AssertionError("network must be skipped")))
    assert probe.main(["--campaign-id", "-123"]) == 2
    out = capsys.readouterr()
    assert json.loads(out.out)["error"] == "invalid_configuration_or_arguments"
    assert out.err == ""


def test_empty_and_type_shapes_do_not_include_remote_record_values():
    for payload in ([], [{"id": 2, "name": PRIVATE, "state": "deleted", "action_payload": 20}]):
        with mocked_tracker({"/offers": payload}) as (client, _):
            result = probe.probe_keitaro_contract(client)
        _assert_clean(result)


def test_offer_stream_preferred_when_redirect_is_first():
    streams = [
        {"id": 20, "name": PRIVATE, "campaign_id": 3, "schema": "redirect", "filters": []},
        {"id": 21, "name": PRIVATE, "campaign_id": 3, "schema": "offers", "offers": []},
    ]
    with mocked_tracker({"/campaigns/3/streams": streams}) as (client, sent):
        report = probe.probe_keitaro_contract(client, campaign_id=3)
    assert report["checks"]["campaign_streams"]["sample"]["known_enums"]["schema"] == "offers"
    assert sent[-1].url.path == f"{BASE}/streams/21"
    assert all(r.method == "GET" for r in sent)
    _assert_clean(report)


def test_empty_stream_list_skips_unnecessary_detail_request():
    with mocked_tracker({"/campaigns/3/streams": []}) as (client, sent):
        report = probe.probe_keitaro_contract(client, campaign_id=3)
    assert report["ok"] is True
    assert report["checks"]["campaign_streams"] == {"status": "empty"}
    assert "stream_detail" not in report["checks"]
    assert sent[-1].url.path == f"{BASE}/campaigns/3/streams"
    _assert_clean(report)


def test_stream_detail_error_does_not_hide_successful_stream_list():
    with mocked_tracker({"/streams/21": httpx.Response(403, text=PRIVATE)}) as (client, sent):
        report = probe.probe_keitaro_contract(client, campaign_id=3)
    assert report["ok"] is False
    assert report["checks"]["campaign_streams"]["status"] == "ok"
    assert report["checks"]["stream_detail"] == {"status": "error", "error": "http", "http_status": 403}
    assert sent[-1].method == "GET"
    _assert_clean(report)


def test_unexpected_internal_error_is_not_serialized_as_remote_data():
    with mocked_tracker() as (client, _):
        def failing_catalog():
            raise RuntimeError(PRIVATE)
        client.list_domains = failing_catalog
        report = probe.probe_keitaro_contract(client)
    assert report["checks"]["domains"] == {"status": "error", "error": "unexpected_local_error"}
    _assert_clean(report)
