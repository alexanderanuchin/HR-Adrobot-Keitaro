"""Exercise the actual workspace HTTP API with PostgreSQL and a fake Keitaro."""

import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.config import get_settings
from app.db.base import Base
from app.db.workspace import operation_session
from app.integrations.keitaro_admin import KeitaroAdminClient
from app.main import app
from app.workspace_api import engine_dependency, keitaro_dependency

PREFIX = "/internal/v1/workspace"
HEADERS = {"X-Internal-Key": "test-only-internal-key", "X-Actor-Id": "9"}


@pytest.fixture
def database():
    url = get_settings().database_url
    admin = create_engine(url, connect_args={"connect_timeout": 3})
    namespace = f"workspace_test_{uuid4().hex}"
    try:
        with admin.begin() as connection:
            connection.execute(CreateSchema(namespace))
    except OperationalError:
        admin.dispose()
        pytest.skip("PostgreSQL is unavailable")
    engine = create_engine(url, connect_args={"options": f"-csearch_path={namespace}"})
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(namespace, cascade=True))
        admin.dispose()


class Tracker:
    def __init__(self):
        self.requests = []
        self.campaigns = {42: {"id": 42, "name": "Demo", "alias": "demo-existing", "state": "active"}}
        self.flows = {
            51: {"id": 51, "campaign_id": 42, "name": "GEO", "type": "regular",
                 "schema": "redirect", "position": 1, "state": "active", "filters": {"kept": True}},
            52: {"id": 52, "campaign_id": 42, "name": "Offers", "type": "default",
                 "schema": "landings", "position": 2, "state": "active",
                 "action_payload": None, "comments": None,
                 "landings": [{"landing_id": 31}], "filters": [{"id": 55, "name": "country"}],
                 "offers": [
                     {"id": 88, "stream_id": 52, "offer_id": 11, "share": 60, "state": "active"},
                     {"id": 89, "stream_id": 52, "offer_id": 12, "share": 40, "state": "active"},
                 ]},
        }
        self.catalog = {
            "offers": [{"id": number, "name": f"Test offer {number}", "state": "active"} for number in (11, 12, 13)],
            "domains": [{"id": 21, "name": "mock.example.invalid", "state": "active"}],
            "groups": [{"id": 22, "name": "Campaigns", "type": "campaigns"}],
            "traffic_sources": [{"id": 23, "name": "Traffic", "state": "active"}],
        }
        self.fail_activation_response = False
        self.fail_stream_creation = False
        self.fail_after_publish = False
        self.drop_publish = False
        self.read_unavailable = False
        self.reject_publish = False
        self.change_routing = False

    @property
    def writes(self):
        return [request for request in self.requests if request.method in {"POST", "PUT"}]

    def handle(self, request):
        self.requests.append(request)
        assert request.headers["Api-Key"] == "test-tracker-key"
        path = request.url.path.removeprefix("/admin_api/v1/")
        traffic = path.split("/")
        if request.method == "GET":
            if path == "campaigns":
                offset = int(request.url.params.get("offset", "0"))
                limit = int(request.url.params.get("limit", "200"))
                return httpx.Response(200, json=list(self.campaigns.values())[offset:offset + limit])
            if path in self.catalog:
                return httpx.Response(200, json=self.catalog[path])
            if traffic[0] == "offers":
                item = next((offer for offer in self.catalog["offers"] if offer["id"] == int(traffic[1])), None)
            elif traffic[0] == "campaigns" and len(traffic) == 3:
                item = [flow for flow in self.flows.values() if flow["campaign_id"] == int(traffic[1])]
            elif traffic[0] == "campaigns":
                item = self.campaigns.get(int(traffic[1]))
            elif traffic[0] == "streams":
                if self.read_unavailable:
                    return httpx.Response(503, json={"error": "simulated read failure"})
                item = self.flows.get(int(traffic[1]))
            else:
                pytest.fail(f"Unexpected mocked path {path}")
            return httpx.Response(200, json=item) if item is not None else httpx.Response(404)

        if request.method == "POST" and path == "campaigns":
            payload = json.loads(request.content)
            entity = {**payload, "id": 43, "group_id": int(payload["group_id"])}
            self.campaigns[43] = entity
            return httpx.Response(200, json=entity)
        if request.method == "POST" and path == "streams":
            payload = json.loads(request.content)
            if self.fail_stream_creation and payload["position"] == 2:
                return httpx.Response(406)
            entity = {"id": 71 + payload["position"], **payload}
            if payload["schema"] == "redirect":
                assert payload["action_payload"] == "https://www.google.com/"
                entity["action_options"] = None
            self.flows[entity["id"]] = entity
            return httpx.Response(200, json=entity)
        if request.method == "POST" and path == "campaigns/43/enable":
            self.campaigns[43]["state"] = "active"
            if self.fail_activation_response:
                raise httpx.ReadTimeout("mock activation reply lost", request=request)
            return httpx.Response(200)
        if request.method == "PUT" and path == "streams/52":
            payload = json.loads(request.content)
            assert set(payload) == {"offers"}
            if self.reject_publish:
                return httpx.Response(406, json={"error": "non-public upstream detail"})
            if self.drop_publish:
                raise httpx.ReadTimeout("mock request lost", request=request)
            self.flows[52]["offers"] = [
                {**offer, "id": index, "stream_id": 52} for index, offer in enumerate(payload["offers"], 101)
            ]
            self.flows[52]["action_payload"] = ""
            self.flows[52]["comments"] = "edited upstream" if self.change_routing else ""
            if self.fail_after_publish:
                self.read_unavailable = True
                raise httpx.ReadTimeout("mock publication reply lost", request=request)
            return httpx.Response(200, json=self.flows[52])
        pytest.fail(f"Unexpected mocked write {request.method} {path}")


@pytest.fixture
def tracker():
    return Tracker()


@pytest.fixture
def api(database, tracker):
    with httpx.Client(transport=httpx.MockTransport(tracker.handle)) as transport:
        client = KeitaroAdminClient(
            base_url="https://tracker.example.invalid", api_key="test-tracker-key", client=transport,
        )
        app.dependency_overrides[engine_dependency] = lambda: database
        app.dependency_overrides[keitaro_dependency] = lambda: client
        try:
            with TestClient(app, headers=HEADERS) as web:
                yield web
        finally:
            app.dependency_overrides.pop(engine_dependency)
            app.dependency_overrides.pop(keitaro_dependency)


def post(api, path, data=None, *, expected=200):
    response = api.post(f"{PREFIX}/{path}", json={} if data is None else data)
    assert response.status_code == expected, response.json()
    return response.json()


def fetch(api):
    return post(api, "campaigns/42/fetch")


def workspace(api, stream_id):
    return post(api, f"streams/{stream_id}/workspace")


def change(api, draft, action, position, **extra):
    return post(api, f"drafts/{draft['draft_id']}/edit", {
        "revision": draft["revision"], "action": action, "position_id": position["position_id"], **extra,
    })


def add(api, draft, offer_id=13):
    return post(api, f"drafts/{draft['draft_id']}/offers", {
        "revision": draft["revision"], "offer_id": offer_id,
    })


def publish(api, draft, *, expected=200):
    return post(api, f"drafts/{draft['draft_id']}/publish", {"revision": draft["revision"]}, expected=expected)


def test_workspace_requires_internal_key_and_limits_responses(api, tracker):
    denied = api.get(f"{PREFIX}/catalog", headers={"X-Internal-Key": "wrong"})
    assert denied.status_code == 403
    assert tracker.requests == []
    catalog = api.get(f"{PREFIX}/catalog").json()
    assert catalog["domains"] == [{"id": 21, "name": "mock.example.invalid"}]
    assert api.get(f"{PREFIX}/offers", params={"q": "13"}).json() == [{"id": 13, "name": "Test offer 13"}]
    assert api.get(f"{PREFIX}/campaigns/remote").json()["items"] == [{"id": 42, "name": "Demo"}]


def test_fetch_edit_publish_archive_and_cancel(api, tracker):
    captured = fetch(api)
    redirect, offer_flow = captured["streams"]
    assert redirect["schema"] == "redirect" and not redirect["editable"]
    assert offer_flow["editable"]
    post(api, f"streams/{redirect['id']}/workspace", expected=409)
    draft = workspace(api, offer_flow["id"])
    assert [row["calculated_percent"] for row in draft["positions"]] == [60, 40]
    stale = draft
    draft = add(api, draft)
    first = draft["positions"][0]
    publish(api, stale, expected=409)
    post(api, f"drafts/{stale['draft_id']}/edit", {
        "revision": stale["revision"], "action": "remove", "position_id": first["position_id"],
    }, expected=409)
    draft = change(api, draft, "remove", first)
    assert draft["positions"][0]["calculated_percent"] == 0
    assert draft["positions"][0]["status"] == "REMOVED"
    draft = change(api, draft, "bring_back", first)
    draft = change(api, draft, "pin", first, pinned_percent=70)
    assert [row["calculated_percent"] for row in draft["positions"]] == [70, 15, 15]
    assert workspace(api, offer_flow["id"]) == draft
    assert fetch(api)["streams"][1]["draft_status"] == "DIRTY"
    assert workspace(api, offer_flow["id"]) == draft
    assert tracker.writes == []

    published = publish(api, draft)
    assert published["status"] == "PUBLISHED"
    assert tracker.flows[52]["landings"] == [{"landing_id": 31}]
    assert tracker.flows[52]["filters"] == [{"id": 55, "name": "country"}]
    assert [entry["share"] for entry in tracker.flows[52]["offers"]] == [70, 15, 15]
    assert publish(api, draft)["status"] == "PUBLISHED"
    assert len(tracker.writes) == 1

    tracker.flows[52]["offers"] = tracker.flows[52]["offers"][:2]
    fetch(api)
    clean = workspace(api, offer_flow["id"])
    assert clean["status"] == "SYNCED"
    assert [row["keitaro_offer_id"] for row in clean["positions"]] == [11, 12, 13]
    assert clean["positions"][-1]["status"] == "REMOVED"
    assert sum(row["calculated_percent"] for row in clean["positions"]) == 100
    edited = add(api, clean, offer_id=11)
    cancelled = post(api, f"drafts/{edited['draft_id']}/cancel", {"revision": edited["revision"]})["draft"]
    assert cancelled["draft_id"] != edited["draft_id"]
    assert cancelled["status"] == "SYNCED" and len(cancelled["positions"]) == 3
    assert len(tracker.writes) == 1


def test_external_offer_conflict_is_blocked_until_cancel(api, tracker):
    stream = fetch(api)["streams"][1]
    draft = add(api, workspace(api, stream["id"]))
    tracker.flows[52]["offers"].pop()
    result = publish(api, draft)
    assert result["status"] == "CONFLICT"
    assert len(result["positions"]) == 3
    assert result["reason"] == "remote_offers_changed"
    assert tracker.writes == []
    cancelled = post(api, f"drafts/{result['draft_id']}/cancel", {"revision": result["revision"]})["draft"]
    assert [row["calculated_percent"] for row in cancelled["positions"]] == [100, 0]
    assert cancelled["positions"][1]["status"] == "REMOVED"


def test_routing_only_edit_rebases_and_publication_preserves_it(api, tracker):
    stream = fetch(api)["streams"][1]
    draft = add(api, workspace(api, stream["id"]))
    tracker.flows[52]["filters"].append({"id": 56, "name": "language"})
    assert publish(api, draft)["status"] == "PUBLISHED"
    assert len(tracker.writes) == 1
    assert tracker.flows[52]["filters"][-1]["name"] == "language"


def test_publish_detects_nonempty_routing_change_after_put(api, tracker):
    stream = fetch(api)["streams"][1]
    draft = add(api, workspace(api, stream["id"]))
    tracker.change_routing = True
    assert publish(api, draft)["status"] == "UNCERTAIN"
    assert fetch(api)["streams"][1]["draft_status"] == "CONFLICT"
    assert len(tracker.writes) == 1


def test_publish_timeout_is_reconciled_without_repeating_put(api, tracker, database):
    stream = fetch(api)["streams"][1]
    draft = add(api, workspace(api, stream["id"]))
    with operation_session(database, "campaign:42"):
        publish(api, draft, expected=409)
    assert tracker.writes == []
    tracker.fail_after_publish = True
    result = publish(api, draft)
    assert result["status"] == "UNCERTAIN"
    assert len(tracker.writes) == 1
    publish(api, result, expected=409)
    tracker.read_unavailable = False
    assert fetch(api)["streams"][1]["draft_status"] is None
    assert workspace(api, stream["id"])["status"] == "SYNCED"
    assert len(tracker.writes) == 1


def test_definite_rejection_preserves_editable_draft(api, tracker):
    stream = fetch(api)["streams"][1]
    draft = add(api, workspace(api, stream["id"]))
    tracker.reject_publish = True
    result = publish(api, draft)
    assert result["status"] == "DIRTY"
    assert result["reason"] == "publication_rejected" and result["upstream_status"] == 406
    assert "non-public" not in str(result)
    assert len(tracker.writes) == 1


def test_uncertain_draft_requires_fetch_and_confirmation_to_discard(api, tracker):
    stream = fetch(api)["streams"][1]
    draft = add(api, workspace(api, stream["id"]))
    tracker.drop_publish = True
    uncertain = publish(api, draft)
    assert uncertain["status"] == "UNCERTAIN"
    path = f"drafts/{draft['draft_id']}/cancel"
    post(api, path, {"revision": uncertain["revision"], "confirm_uncertain": True}, expected=409)
    fetch(api)
    observed = workspace(api, stream["id"])
    assert observed["status"] == "UNCERTAIN" and observed["reconciliation_checked"]
    post(api, path, {"revision": observed["revision"]}, expected=409)
    clean = post(api, path, {"revision": observed["revision"], "confirm_uncertain": True})["draft"]
    assert clean["status"] == "SYNCED" and len(clean["positions"]) == 2
    assert len(tracker.writes) == 1


def test_publish_empty_offer_list_keeps_local_archive(api, tracker):
    stream = fetch(api)["streams"][1]
    draft = workspace(api, stream["id"])
    for position in draft["positions"]:
        draft = change(api, draft, "remove", position)
    assert publish(api, draft)["status"] == "PUBLISHED"
    assert json.loads(tracker.writes[0].content) == {"offers": []}
    reopened = workspace(api, stream["id"])
    assert [row["status"] for row in reopened["positions"]] == ["REMOVED", "REMOVED"]


@pytest.mark.parametrize("fault,expected,reconciled", [
    (None, "COMPLETED", "COMPLETED"),
    ("activation", "UNCERTAIN", "COMPLETED"),
    ("second stream", "UNCERTAIN", "UNCERTAIN"),
])
def test_create_campaign_replays_by_alias_without_duplicate_writes(api, tracker, fault, expected, reconciled):
    tracker.fail_activation_response = fault == "activation"
    tracker.fail_stream_creation = fault == "second stream"
    fields = {
        "name": "New campaign", "alias": "new-au-test", "geo": "au",
        "domain_id": 21, "group_id": 22, "traffic_source_id": 23, "offer_id": 11,
    }
    result = post(api, "campaigns", fields)
    assert result["status"] == expected and result["campaign_id"] == 43
    original_writes = tracker.writes.copy()
    assert len(original_writes) == (3 if fault == "second stream" else 4)
    assert post(api, "campaigns", {**fields, "geo": "AU"})["status"] == reconciled
    post(api, "campaigns", {**fields, "offer_id": 12}, expected=409)
    assert tracker.writes == original_writes
    candidates = api.get(f"{PREFIX}/campaigns").json()
    assert candidates == ([{"id": 43, "name": "New campaign"}] if reconciled == "COMPLETED" else [])
