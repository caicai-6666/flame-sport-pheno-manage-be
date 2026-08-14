"""封装赛季统计所需的只读数据库聚合查询。"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass(frozen=True, slots=True)
class CurrentSeasonParticipant:
    season_user_id: int
    user_id: str
    level_id: int
    level_name: str


@dataclass(frozen=True, slots=True)
class CurrentSeasonStatistics:
    id: int
    name: str
    start_date: date
    end_date: date
    required_project_count: int
    status: int
    participants: tuple[CurrentSeasonParticipant, ...]


@dataclass(frozen=True, slots=True)
class CurrentSeasonProjectParticipant:
    user_id: str
    completion_progress: Decimal


class MultipleActiveSeasonsError(RuntimeError):
    """数据库同时存在多个激活赛季，无法确定唯一当前赛季。"""


# 查询唯一激活赛季，并返回正式参赛记录主键、用户和已锁定等级。
async def fetch_current_season_statistics(
    session: AsyncSession,
) -> CurrentSeasonStatistics | None:
    result = await session.exec(
        text(
            """
            SELECT
                season.id AS season_id,
                season.name AS season_name,
                season.start_date,
                season.end_date,
                season.required_project_count,
                season.status AS season_status,
                season_user.id AS season_user_id,
                season_user.user_id,
                season_user.level_id,
                project_level.name AS level_name
            FROM season
            LEFT JOIN season_user
                ON season_user.season_id = season.id
                AND season_user.status >= season.required_project_count
                AND season_user.level_id IS NOT NULL
            LEFT JOIN project_level
                ON project_level.id = season_user.level_id
            WHERE season.status = 1
            ORDER BY season.id ASC, season_user.id ASC
            """
        )
    )
    rows = result.mappings().all()
    if not rows:
        return None

    active_season_ids = {int(row["season_id"]) for row in rows}
    if len(active_season_ids) > 1:
        raise MultipleActiveSeasonsError

    first_row = rows[0]
    participants = tuple(
        CurrentSeasonParticipant(
            season_user_id=int(row["season_user_id"]),
            user_id=str(row["user_id"]),
            level_id=int(row["level_id"]),
            level_name=str(row["level_name"]),
        )
        for row in rows
        if row["season_user_id"] is not None
        and row["user_id"] is not None
        and row["level_id"] is not None
        and row["level_name"] is not None
    )
    return CurrentSeasonStatistics(
        id=int(first_row["season_id"]),
        name=str(first_row["season_name"]),
        start_date=first_row["start_date"],
        end_date=first_row["end_date"],
        required_project_count=int(first_row["required_project_count"]),
        status=int(first_row["season_status"]),
        participants=participants,
    )


# 查询当前赛季指定参赛记录的有效锁定项目，并返回用户及精确完成进度。
async def fetch_current_season_project_participants(
    session: AsyncSession,
    season_user_id: int,
    project_id: int,
) -> tuple[CurrentSeasonProjectParticipant, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                season_user.user_id,
                season_user_project.completion_progress
            FROM season_user_project
            JOIN season_user
                ON season_user.id = season_user_project.season_user_id
            JOIN season
                ON season.id = season_user.season_id
                AND season.status = 1
            WHERE season_user_project.season_user_id = :season_user_id
                AND season_user_project.project_id = :project_id
                AND season_user_project.status = 1
                AND season_user.status >= season.required_project_count
                AND season_user.level_id IS NOT NULL
            ORDER BY season_user_project.id ASC
            """
        ),
        params={
            "season_user_id": season_user_id,
            "project_id": project_id,
        },
    )
    return tuple(
        CurrentSeasonProjectParticipant(
            user_id=str(row["user_id"]),
            completion_progress=Decimal(str(row["completion_progress"])),
        )
        for row in result.mappings().all()
    )
