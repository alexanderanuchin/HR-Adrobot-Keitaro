from django.contrib import admin
from django.urls import path, re_path
from gateway.views import health, integration_health, workspace_logout, workspace_proxy, workspace_session

workspace_resources = (
    r"catalog|offers|campaigns(?:/remote|/[0-9]+(?:/fetch)?)?"
    r"|streams/(?:history|[0-9a-f-]{36}/(?:workspace|refresh))|drafts/[0-9a-f-]{36}/(?:offers|edit|publish|cancel)"
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health/", health, name="health"),
    path("api/v1/integration-health/", integration_health, name="integration-health"),
    path("api/v1/session/", workspace_session, name="workspace-session"),
    path("api/v1/session/logout/", workspace_logout, name="workspace-logout"),
    re_path(rf"^api/v1/workspace/(?P<resource>{workspace_resources})/$",
            workspace_proxy, name="workspace-proxy"),
]
