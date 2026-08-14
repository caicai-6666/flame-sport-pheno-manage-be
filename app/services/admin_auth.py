"""编排管理员密钥登录与访问令牌签发。"""

from dataclasses import dataclass

from app.core.admin_auth import AdminTokenCache


class InvalidAdminKeyError(RuntimeError):
    """管理员密钥不匹配，不能签发访问令牌。"""


@dataclass(frozen=True, slots=True)
class IssuedAdminToken:
    access_token: str
    expires_in: int


# 校验管理员密钥并签发短期令牌，隐藏缓存实现与失败判断细节。
async def issue_admin_token(
    token_cache: AdminTokenCache,
    admin_key: str,
) -> IssuedAdminToken:
    token = await token_cache.issue(admin_key)
    if token is None:
        raise InvalidAdminKeyError
    return IssuedAdminToken(
        access_token=token,
        expires_in=token_cache.ttl_seconds,
    )
