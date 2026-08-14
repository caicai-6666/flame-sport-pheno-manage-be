"""维护管理端路由跨模块复用的认证依赖。"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.clients.client_backend import ClientBackendClient
from app.core.admin_auth import AdminTokenCache
from app.core.config import get_settings

settings = get_settings()
admin_token_cache = AdminTokenCache(
    admin_key=settings.admin_key,
    ttl_seconds=settings.admin_token_ttl_seconds,
    max_size=settings.admin_token_cache_max_size,
)
bearer_scheme = HTTPBearer(auto_error=False)
admin_login_path = f"{settings.public_api_prefix}/auth/login"


# 从应用生命周期状态中取得共享客户端，保证内部请求复用连接池而非逐请求新建客户端。
def get_client_backend(request: Request) -> ClientBackendClient:
    return request.app.state.client_backend


ClientBackend = Annotated[ClientBackendClient, Depends(get_client_backend)]


# 返回进程级令牌缓存，便于路由共享状态并在测试中替换为隔离实例。
def get_admin_token_cache() -> AdminTokenCache:
    return admin_token_cache


# 将未登录请求安全重定向到登录接口，使用 303 避免原请求方法和请求体被重放。
def redirect_to_admin_login(detail: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        detail=detail,
        headers={"Location": admin_login_path},
    )


# 通过统一依赖查询 token 缓存，未命中或已过期时重定向到管理员登录接口。
async def require_admin_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer_scheme),
    ],
    token_cache: Annotated[AdminTokenCache, Depends(get_admin_token_cache)],
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        redirect_to_admin_login("缺少有效的管理员访问令牌")

    if not await token_cache.validate(credentials.credentials):
        redirect_to_admin_login("管理员访问令牌无效或已过期")
