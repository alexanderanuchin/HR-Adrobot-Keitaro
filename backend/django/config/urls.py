from django.contrib import admin
from django.urls import path
from gateway.views import health, integration_health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health/", health, name="health"),
    path("api/v1/integration-health/", integration_health, name="integration-health"),
]
