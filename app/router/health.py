from fastapi import APIRouter

from app.core.config import get_settings
from app.schemas.health import HealthResponse

router = APIRouter(prefix="/health", tags=["health"])
settings = get_settings()


# 返回管理端后端进程的存活状态，不触发数据库或外部服务访问。
@router.get("", response_model=HealthResponse, summary="服务存活检查")
async def get_health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.app_env,
    )
