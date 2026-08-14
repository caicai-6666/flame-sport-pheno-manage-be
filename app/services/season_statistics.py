"""编排当前赛季统计查询及一致性异常。"""

from sqlmodel.ext.asyncio.session import AsyncSession

from app.repositories.season_statistics import (
    CurrentSeasonProjectParticipant,
    CurrentSeasonStatistics,
    MultipleActiveSeasonsError,
    fetch_current_season_project_participants,
    fetch_current_season_statistics,
)


class CurrentSeasonNotFoundError(RuntimeError):
    """当前没有处于激活状态的赛季。"""


class CurrentSeasonConflictError(RuntimeError):
    """存在多个激活赛季，无法确定唯一当前赛季。"""


# 在只读事务中获取唯一当前赛季，并把仓储异常转换为稳定的应用用例异常。
async def get_current_season_statistics(
    session: AsyncSession,
) -> CurrentSeasonStatistics:
    try:
        async with session.begin():
            current_season = await fetch_current_season_statistics(session)
    except MultipleActiveSeasonsError as error:
        raise CurrentSeasonConflictError from error

    if current_season is None:
        raise CurrentSeasonNotFoundError
    return current_season


# 在只读事务中查询当前赛季指定用户项目，保持筛选和事务逻辑脱离 HTTP 层。
async def get_current_season_project_participants(
    session: AsyncSession,
    season_user_id: int,
    project_id: int,
) -> tuple[CurrentSeasonProjectParticipant, ...]:
    async with session.begin():
        return await fetch_current_season_project_participants(
            session,
            season_user_id,
            project_id,
        )
