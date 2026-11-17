from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model

class GatewayTests(TestCase):
    def test_health(self):
        self.assertEqual(self.client.get(reverse("health")).status_code, 200)

    def test_integration_health_requires_authentication(self):
        self.assertIn(self.client.get(reverse("integration-health")).status_code, (302, 403))

    def test_custom_user_model(self):
        user = get_user_model().objects.create_user(username="tester", password="test-user-password")
        self.assertEqual(user.username, "tester")
