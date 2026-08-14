from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.clients.client_backend import ClientBackendClient
from app.core.config import get_settings
from app.db.session import engine
from app.router import router

settings = get_settings()


# 管理应用级共享资源，并确保 HTTP 客户端和数据库连接池在退出时可靠释放。
@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.client_backend = ClientBackendClient(
        base_url=str(settings.client_backend_base_url),
        timeout_seconds=settings.client_backend_timeout_seconds,
    )
    try:
        yield
    finally:
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
