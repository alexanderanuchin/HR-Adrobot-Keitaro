"""Initial gateway checks. Business endpoints will be introduced in later commits."""
import httpx
from django.conf import settings
from django.db import DatabaseError, connection
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

@api_view(["GET"])
@permission_classes([])
def health(request):
    try:
        connection.ensure_connection()
    except DatabaseError:
        return JsonResponse({"service": "django", "status": "unavailable"}, status=503)
    return JsonResponse({"service": "django", "status": "ok"})

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def integration_health(request):
    if not settings.INTERNAL_API_KEY:
        return JsonResponse({"detail": "Internal API authentication is not configured"}, status=503)
    try:
        response = httpx.get(
            f"{settings.FASTAPI_INTERNAL_URL}/internal/v1/status",
            headers={"X-Internal-Key": settings.INTERNAL_API_KEY},
            timeout=4.0,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return JsonResponse({"detail": "FastAPI is unavailable"}, status=503)
    return JsonResponse({"gateway": "django", "fastapi": response.json()})
