"""提供管理员登录及令牌会话校验接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.admin_auth import AdminTokenCache
from app.router.dependencies import get_admin_token_cache, require_admin_token
from app.schemas.admin_auth import (
    AdminLoginRequest,
    AdminLoginResponse,
    AdminSessionResponse,
)
from app.services.admin_auth import InvalidAdminKeyError, issue_admin_token

router = APIRouter(prefix="/auth", tags=["admin-authentication"])


# 校验启动环境中的管理员密钥，并向前端签发用于后续请求的短期 Bearer Token。
@router.post(
    "/login",
    response_model=AdminLoginResponse,
    summary="管理员登录",
)
async def login_admin(
    request: AdminLoginRequest,
    token_cache: Annotated[AdminTokenCache, Depends(get_admin_token_cache)],
) -> AdminLoginResponse:
    try:
        issued_token = await issue_admin_token(
            token_cache,
            request.admin_key.get_secret_value(),
        )
    except InvalidAdminKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="管理员密钥无效",
        ) from error

    return AdminLoginResponse(
        access_token=issued_token.access_token,
        expires_in=issued_token.expires_in,
    )


# 验证前端缓存的访问令牌仍然有效，不返回管理员密钥或缓存内部信息。
@router.get(
    "/session",
    response_model=AdminSessionResponse,
    summary="验证管理员访问令牌",
    dependencies=[Depends(require_admin_token)],
)
async def get_admin_session() -> AdminSessionResponse:
    return AdminSessionResponse()
