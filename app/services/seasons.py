"""编排管理端赛季查询与创建用例。"""

from dataclasses import dataclass
from calendar import monthrange
from datetime import date, timedelta
from enum import IntEnum

from sqlmodel.ext.asyncio.session import AsyncSession

from app.repositories.projects import lock_visible_project_count
from app.repositories.seasons import (
    fetch_all_seasons,
    insert_season,
    lock_latest_season_boundary,
)


class SeasonStatus(IntEnum):
    NOT_STARTED = 0
    ACTIVE = 1
    SETTLING = 2
    ENDED = 3


SEASON_STATUS_NAMES: dict[SeasonStatus, str] = {
    SeasonStatus.NOT_STARTED: "未开始",
    SeasonStatus.ACTIVE: "进行中",
    SeasonStatus.SETTLING: "结算中",
    SeasonStatus.ENDED: "已结束",
}


@dataclass(frozen=True, slots=True)
class SeasonListItem:
    id: int
    name: str
    start_date: date
    end_date: date
    status: int
    status_name: str


@dataclass(frozen=True, slots=True)
class CreatedSeason:
    id: int
    name: str
    start_date: date
    end_date: date
    required_project_count: int
    status: int
    status_name: str


class UnknownSeasonStatusError(RuntimeError):
    """数据库包含未定义的赛季状态，无法生成可靠的状态含义。"""


class InvalidSeasonDateRangeError(ValueError):
    """新增赛季的起止日期不足一个完整日历月。"""


class SeasonStartDateConflictError(ValueError):
    """新增赛季开始日期没有晚于已有赛季的最晚结束日期。"""


class InsufficientVisibleProjectsError(ValueError):
    """赛季要求项目数超过当前启用项目总数。"""


# 将数据库状态值转换为统一中文含义，未知状态按一致性异常处理。
def resolve_season_status_name(status: int) -> str:
    try:
        season_status = SeasonStatus(status)
    except ValueError as error:
        raise UnknownSeasonStatusError from error
    return SEASON_STATUS_NAMES[season_status]


# 计算包含首尾日期的一个完整日历月所允许的最早结束日期。
def calculate_minimum_season_end_date(start_date: date) -> date:
    if start_date.month == 12:
        next_year = start_date.year + 1
        next_month = 1
    else:
        next_year = start_date.year
        next_month = start_date.month + 1
    next_month_day = min(
        start_date.day,
        monthrange(next_year, next_month)[1],
    )
    next_month_anniversary = date(
        next_year,
        next_month,
        next_month_day,
    )
    return next_month_anniversary - timedelta(days=1)


# 在只读事务内获取全部赛季，并为每条记录补充统一的状态中文含义。
async def list_seasons(
    session: AsyncSession,
) -> tuple[SeasonListItem, ...]:
    async with session.begin():
        records = await fetch_all_seasons(session)
    return tuple(
        SeasonListItem(
            id=record.id,
            name=record.name,
            start_date=record.start_date,
            end_date=record.end_date,
            status=record.status,
            status_name=resolve_season_status_name(record.status),
        )
        for record in records
    )


# 校验日期边界及可见项目容量，并在同一事务中原子创建未开始赛季。
async def create_season(
    session: AsyncSession,
    name: str,
    start_date: date,
    end_date: date,
    required_project_count: int,
) -> CreatedSeason:
    minimum_end_date = calculate_minimum_season_end_date(start_date)
    if end_date < minimum_end_date:
        raise InvalidSeasonDateRangeError

    async with session.begin():
        latest_boundary = await lock_latest_season_boundary(session)
        if (
            latest_boundary is not None
            and start_date <= latest_boundary.end_date
        ):
            raise SeasonStartDateConflictError
        visible_project_count = await lock_visible_project_count(session)
        if required_project_count > visible_project_count:
            raise InsufficientVisibleProjectsError
        record = await insert_season(
            session,
            name,
            start_date,
            end_date,
            required_project_count,
        )

    return CreatedSeason(
        id=record.id,
        name=record.name,
        start_date=record.start_date,
        end_date=record.end_date,
        required_project_count=record.required_project_count,
        status=record.status,
        status_name=resolve_season_status_name(record.status),
    )
