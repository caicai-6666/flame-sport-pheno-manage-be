import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.query_manager import AgentQueryManager
from app.clients.client_backend import ClientBackendClient
from app.core.config import get_settings
from app.db.session import async_session_factory, engine
from app.jobs.season_status import run_season_status_scheduler
from app.router import router

settings = get_settings()


# 管理应用级共享资源和赛季结算任务，并在退出时按依赖顺序可靠释放。
@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.client_backend = ClientBackendClient(
        base_url=str(settings.client_backend_base_url),
        timeout_seconds=settings.client_backend_timeout_seconds,
    )
    application.state.agent_query_manager = AgentQueryManager(settings)
    season_status_task: asyncio.Task[None] | None = None
    if settings.season_status_check_enabled:
        season_status_task = asyncio.create_task(
            run_season_status_scheduler(
                async_session_factory,
                application.state.client_backend,
                settings.season_status_check_interval_seconds,
                settings.season_settlement_review_batch_size,
                settings.season_settlement_review_concurrency,
                settings.season_settlement_user_batch_size,
                settings.season_settlement_auto_complete_enabled,
                settings.season_settlement_auto_complete_after_days,
            ),
            name="season-status-scheduler",
        )
    try:
        yield
    finally:
        await application.state.agent_query_manager.shutdown()
        if season_status_task is not None:
            season_status_task.cancel()
            with suppress(asyncio.CancelledError):
                await season_status_task
        await application.state.client_backend.aclose()
        await engine.dispose()


# 组装 FastAPI 应用、中间件和管理端路由，保持入口层不承载业务规则。
def create_app() -> FastAPI:
    application = FastAPI(
        title=settings.app_name,
        debug=settings.app_debug,
        lifespan=lifespan,
    )

    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    application.include_router(router, prefix=settings.api_prefix)
    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_debug,
    )
