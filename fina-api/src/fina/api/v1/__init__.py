"""Version 1 business API routers."""

from fastapi import APIRouter, Depends

from fina.api.v1.client_profile import router as client_profile_router
from fina.api.v1.orders import router as orders_router
from fina.api.v1.transcripts import router as transcripts_router
from fina.security.mcp import require_mcp_authentication

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_mcp_authentication)],
)
router.include_router(transcripts_router)
router.include_router(orders_router)
router.include_router(client_profile_router)
