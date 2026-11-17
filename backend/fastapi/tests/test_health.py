from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_live():
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"service": "fastapi", "status": "ok"}

def test_internal_status_requires_key():
    assert client.get("/internal/v1/status").status_code == 403
    assert client.get("/internal/v1/status", headers={"X-Internal-Key": "invalid"}).status_code == 403

def test_internal_status_authorized():
    response = client.get("/internal/v1/status", headers={"X-Internal-Key": "test-only-internal-key"})
    assert response.status_code == 200
    assert response.json()["integration"] == "not-implemented"
