"""封装管理端赛季基础查询、并发边界锁定与写入。"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass(frozen=True, slots=True)
class SeasonRecord:
    id: int
    name: str
    start_date: date
    end_date: date
    status: int


@dataclass(frozen=True, slots=True)
class LatestSeasonBoundary:
    id: int
    end_date: date


@dataclass(frozen=True, slots=True)
class CreatedSeasonRecord:
    id: int
    name: str
    start_date: date
    end_date: date
    required_project_count: int
    status: int


# 查询全部赛季的基础时间信息，不按状态过滤，并以最近开始的赛季优先返回。
async def fetch_all_seasons(
    session: AsyncSession,
) -> tuple[SeasonRecord, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                season.id,
                season.name,
                season.start_date,
                season.end_date,
                season.status
            FROM season
            ORDER BY
                season.start_date DESC,
                season.end_date DESC,
                season.id DESC
            """
        )
    )
    return tuple(
        SeasonRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            start_date=row["start_date"],
            end_date=row["end_date"],
            status=int(row["status"]),
        )
        for row in result.mappings().all()
    )


# 锁定结束日期最晚的赛季，串行化“校验边界并创建赛季”的并发写入。
async def lock_latest_season_boundary(
    session: AsyncSession,
) -> LatestSeasonBoundary | None:
    result = await session.exec(
        text(
            """
            SELECT
                season.id,
                season.end_date
            FROM season
            ORDER BY season.end_date DESC, season.id DESC
            LIMIT 1
            FOR UPDATE
            """
        )
    )
    row = result.mappings().first()
    if row is None:
        return None
    return LatestSeasonBoundary(
        id=int(row["id"]),
        end_date=row["end_date"],
    )


# 按请求写入必选项目数量和默认未开始状态，并返回数据库生成的主键。
async def insert_season(
    session: AsyncSession,
    name: str,
    start_date: date,
    end_date: date,
    required_project_count: int,
) -> CreatedSeasonRecord:
    result = await session.exec(
        text(
            """
            INSERT INTO season (
                name,
                start_date,
                end_date,
                required_project_count,
                status
            ) VALUES (
                :name,
                :start_date,
                :end_date,
                :required_project_count,
                0
            )
            """
        ),
        params={
            "name": name,
            "start_date": start_date,
            "end_date": end_date,
            "required_project_count": required_project_count,
        },
    )
    return CreatedSeasonRecord(
        id=int(result.lastrowid),
        name=name,
        start_date=start_date,
        end_date=end_date,
        required_project_count=required_project_count,
        status=0,
    )
