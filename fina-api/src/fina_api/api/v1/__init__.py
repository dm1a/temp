"""Version 1 business API routers."""
from fastapi import APIRouter, Depends

from fina_api.api.v1.client_profile import router as client_profile_router
from fina_api.api.v1.deals import router as deals_router
from fina_api.api.v1.transcripts import router as transcripts_router
from fina_api.security.mcp import require_mcp_authentication

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_mcp_authentication)],
)
router.include_router(transcripts_router)
router.include_router(deals_router)
router.include_router(client_profile_router)
