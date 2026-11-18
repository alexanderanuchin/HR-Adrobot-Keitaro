"""Internal Keitaro integration API."""
from hmac import compare_digest
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from app.core.config import Settings, get_settings
from app.db.health import database_ready
from app.db.workspace import OperationBusy
from app.integrations.keitaro_admin import (
    KeitaroConfigurationError, KeitaroError, KeitaroHTTPError, KeitaroNotFoundError,
    KeitaroRateLimitError, KeitaroRequestError, KeitaroTimeoutError, KeitaroValidationError,
)
from app.services.campaign_preflight import CampaignPreflightError
from app.services.draft_editor import DraftEditBlocked, DraftEditError, DraftEditNotFound, DraftRevisionConflict
from app.services.draft_workspace import DraftWorkspaceError, DraftWorkspaceNotFound
from app.workspace_api import router as workspace_router

app = FastAPI(title="Adrobot Integration API", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)

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
    if not settings.internal_api_key or not x_internal_key or not compare_digest(
        x_internal_key.encode(), settings.internal_api_key.encode(),
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

@app.get("/internal/v1/status", dependencies=[Depends(verify_internal_key)])
def internal_status(settings: Settings = Depends(get_settings)) -> dict[str, str | bool]:
    return {
        "service": "fastapi",
        "status": "ok",
        "keitaro_configured": bool(settings.keitaro_api_key),
        "integration": "keitaro-admin-api",
    }


async def _workspace_error(_request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, (DraftWorkspaceNotFound, DraftEditNotFound, KeitaroNotFoundError)):
        status = 404
    elif isinstance(exc, (OperationBusy, DraftRevisionConflict, DraftEditBlocked)):
        status = 409
    elif isinstance(exc, (CampaignPreflightError, DraftEditError, DraftWorkspaceError,
                          KeitaroRequestError, KeitaroValidationError)):
        status = 422
    elif isinstance(exc, (KeitaroConfigurationError, KeitaroRateLimitError)):
        status = 503
    elif isinstance(exc, KeitaroTimeoutError):
        status = 504
    else:
        status = 502
    body = {"detail": str(exc)}
    if isinstance(exc, KeitaroHTTPError):
        body["upstream_status"] = exc.status_code
    return JSONResponse(body, status_code=status)


@app.exception_handler(RequestValidationError)
async def invalid_input(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # Field values can contain untrusted text. Expose only validation metadata.
    errors = [{"field": ".".join(map(str, row["loc"])), "type": row["type"]} for row in exc.errors()]
    return JSONResponse({"detail": "Invalid request fields", "errors": errors}, status_code=422)


for error_type in (
    OperationBusy, KeitaroError, CampaignPreflightError, DraftEditError,
    DraftEditNotFound, DraftWorkspaceError, DraftWorkspaceNotFound,
):
    app.add_exception_handler(error_type, _workspace_error)

app.include_router(workspace_router, dependencies=[Depends(verify_internal_key)])
