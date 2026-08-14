"""编排管理端用户基础信息查询用例。"""

from sqlmodel.ext.asyncio.session import AsyncSession

from app.repositories.users import (
    UserBasicInformation,
    fetch_user_basic_information,
)


# 去除重复用户 ID 并保留首次出现顺序，避免重复查询和重复响应。
def deduplicate_user_ids(user_ids: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(user_ids))


# 去重用户 ID 后在只读事务中查询基础信息，不存在的用户不生成占位记录。
async def get_user_basic_information(
    session: AsyncSession,
    user_ids: list[str],
) -> tuple[UserBasicInformation, ...]:
    unique_user_ids = deduplicate_user_ids(user_ids)
    async with session.begin():
        return await fetch_user_basic_information(session, unique_user_ids)
