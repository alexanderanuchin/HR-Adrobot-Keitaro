"""Internal-only FastAPI skeleton; no Keitaro mutations in scaffold."""
from hmac import compare_digest
from fastapi import Depends, FastAPI, Header, HTTPException
from app.core.config import Settings, get_settings
from app.db.health import database_ready

app = FastAPI(title="Adrobot Integration API", version="0.1.0", docs_url=None, redoc_url=None)

@app.get("/health/live")
def live() -> dict[str, str]:
    return {"service": "fastapi", "status": "ok"}

@app.get("/health/ready")
def ready() -> dict[str, str]:
    try:
        if database_ready():
            return {"service": "fastapi", "status": "ready"}
    except Exception:
        pass
    raise HTTPException(status_code=503, detail="Database unavailable")

def verify_internal_key(
    x_internal_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.internal_api_key or not x_internal_key or not compare_digest(x_internal_key, settings.internal_api_key):
        raise HTTPException(status_code=403, detail="Forbidden")

@app.get("/internal/v1/status", dependencies=[Depends(verify_internal_key)])
def internal_status(settings: Settings = Depends(get_settings)) -> dict[str, str | bool]:
    return {
        "service": "fastapi",
        "status": "ok",
        "keitaro_configured": bool(settings.keitaro_api_key),
        "integration": "not-implemented",
    }
