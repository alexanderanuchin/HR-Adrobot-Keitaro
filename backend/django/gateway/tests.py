from unittest.mock import patch

import httpx
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings


@override_settings(INTERNAL_API_KEY="test-internal-key", FASTAPI_INTERNAL_URL="http://fastapi:8000")
class WorkspaceGatewayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.staff = users.create_user(username="operator", password="fake-test-password", is_staff=True)
        cls.viewer = users.create_user(username="viewer", password="fake-viewer-password")

    def test_gateway_requires_staff_and_only_routes_known_resources(self):
        with patch("gateway.views.httpx.request") as upstream:
            self.assertEqual(self.client.get("/api/v1/workspace/campaigns/").status_code, 403)
            self.client.force_login(self.viewer)
            self.assertEqual(self.client.get("/api/v1/workspace/campaigns/").status_code, 403)
            self.client.force_login(self.staff)
            self.assertEqual(self.client.get("/api/v1/workspace/status/").status_code, 404)
            upstream.assert_not_called()

    def test_session_login_and_writes_require_csrf_and_forward_server_actor(self):
        client = Client(enforce_csrf_checks=True)
        token = client.get("/api/v1/session/").json()["csrf_token"]
        credentials = {"username": "operator", "password": "fake-test-password"}
        self.assertEqual(client.post("/api/v1/session/", credentials, content_type="application/json").status_code, 403)
        signed_in = client.post("/api/v1/session/", credentials, content_type="application/json",
                                HTTP_X_CSRFTOKEN=token)
        self.assertEqual(signed_in.json()["username"], "operator")
        rotated = signed_in.json()["csrf_token"]
        route = "/api/v1/workspace/campaigns/42/fetch/"
        with patch("gateway.views.httpx.request", return_value=httpx.Response(200, json={"streams": []})) as upstream:
            self.assertEqual(client.post(route, {}, content_type="application/json").status_code, 403)
            upstream.assert_not_called()
            response = client.post(route, {}, content_type="application/json",
                                   HTTP_X_CSRFTOKEN=rotated, HTTP_X_ACTOR_ID="999")
            self.assertEqual(response.json(), {"streams": []})
            args, kwargs = upstream.call_args
            self.assertEqual(args, ("POST", "http://fastapi:8000/internal/v1/workspace/campaigns/42/fetch"))
            self.assertEqual(kwargs["headers"], {"X-Internal-Key": "test-internal-key", "X-Actor-Id": str(self.staff.pk)})
            self.assertFalse(kwargs["follow_redirects"])
        self.assertEqual(client.post("/api/v1/session/logout/", HTTP_X_CSRFTOKEN=rotated).json()["username"], None)
        self.assertEqual(client.get(route).status_code, 403)

    def test_forwards_query_and_returns_json_errors(self):
        self.client.force_login(self.staff)
        reply = httpx.Response(422, json={"detail": "Invalid request fields"})
        with patch("gateway.views.httpx.request", return_value=reply) as upstream:
            response = self.client.get("/api/v1/workspace/offers/", {"q": "example offer"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "Invalid request fields"})
        self.assertEqual(upstream.call_args.kwargs["params"], {"q": ["example offer"]})

    def test_stream_history_is_available_to_staff(self):
        route = "/api/v1/workspace/streams/history/"
        self.assertEqual(self.client.get(route).status_code, 403)
        self.client.force_login(self.staff)
        page = {"items": [], "next_offset": None}
        with patch("gateway.views.httpx.request", return_value=httpx.Response(200, json=page)) as upstream:
            self.assertEqual(self.client.get(route, {"offset": 50}).json(), page)
        self.assertEqual(upstream.call_args.args, ("GET", "http://fastapi:8000/internal/v1/workspace/streams/history"))
        self.assertEqual(upstream.call_args.kwargs["params"], {"offset": ["50"]})
