"""聚合管理端公开路由和统一鉴权后的业务路由。"""

from fastapi import APIRouter, Depends

from app.router import (
    admin_auth,
    health,
    image,
    product,
    project,
    project_level,
    proof,
    season,
    season_statistics,
    settlement,
    suggestion,
    user,
)
from app.router.dependencies import require_admin_token

router = APIRouter()
router.include_router(health.router)
router.include_router(admin_auth.router)

protected_router = APIRouter(dependencies=[Depends(require_admin_token)])
protected_router.include_router(season.router)
protected_router.include_router(season_statistics.router)
protected_router.include_router(settlement.router)
protected_router.include_router(user.router)
protected_router.include_router(image.router)
protected_router.include_router(product.router)
protected_router.include_router(project.router)
protected_router.include_router(project_level.router)
protected_router.include_router(proof.router)
protected_router.include_router(suggestion.router)
router.include_router(protected_router)
