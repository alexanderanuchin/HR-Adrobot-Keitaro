import os
import pytest
from app.core.config import get_settings

@pytest.fixture(autouse=True)
def deterministic_internal_api_key(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-only-internal-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
