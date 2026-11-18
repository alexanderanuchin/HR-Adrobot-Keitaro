"""Django session authentication and the internal Keitaro gateway."""
import json

import httpx
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_POST
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser, IsAuthenticated

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


def _session_response(request):
    user = request.user
    return JsonResponse({
        "username": user.get_username() if user.is_authenticated and user.is_staff else None,
        "csrf_token": get_token(request),
    })


@require_http_methods(["GET", "POST"])
@csrf_protect
def workspace_session(request):
    if request.method == "GET":
        return _session_response(request)
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeError):
        return JsonResponse({"detail": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"detail": "Invalid credentials"}, status=400)
    username, password = payload.get("username"), payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
        return JsonResponse({"detail": "Invalid credentials"}, status=400)
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_staff:
        return JsonResponse({"detail": "Неверные данные или нет доступа к рабочему пространству"}, status=403)
    login(request, user)
    return _session_response(request)


@require_POST
@csrf_protect
def workspace_logout(request):
    logout(request)
    return _session_response(request)


@api_view(["GET", "POST"])
@permission_classes([IsAdminUser])
def workspace_proxy(request, resource):
    if not settings.INTERNAL_API_KEY:
        return JsonResponse({"detail": "Internal API authentication is not configured"}, status=503)
    try:
        response = httpx.request(
            request.method, f"{settings.FASTAPI_INTERNAL_URL}/internal/v1/workspace/{resource}",
            params=dict(request.query_params.lists()) if request.query_params else None,
            json=request.data if request.method == "POST" else None,
            headers={"X-Internal-Key": settings.INTERNAL_API_KEY, "X-Actor-Id": str(request.user.pk)},
            timeout=180.0 if request.method == "POST" else 45.0,
            trust_env=False,
            follow_redirects=False,
        )
        if response.status_code >= 500 and response.headers.get("content-type") != "application/json":
            return JsonResponse({"detail": "FastAPI is unavailable"}, status=502)
        data = response.json()
    except httpx.TimeoutException:
        return JsonResponse({"detail": "FastAPI timed out. Refresh to check the operation result."}, status=504)
    except (httpx.HTTPError, ValueError):
        return JsonResponse({"detail": "FastAPI is unavailable"}, status=502)
    return JsonResponse(data, safe=isinstance(data, dict), status=response.status_code)
