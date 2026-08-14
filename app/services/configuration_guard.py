"""统一执行激活赛季期间高影响业务配置的时间窗口校验。"""

from datetime import date, datetime, time, timedelta

from sqlmodel.ext.asyncio.session import AsyncSession

from app.repositories.configuration_guard import (
    lock_active_seasons_for_configuration,
)


class MultipleActiveSeasonsForConfigurationError(RuntimeError):
    """存在多个进行中赛季，无法确定唯一配置窗口。"""


class ActiveSeasonConfigurationWindowClosedError(RuntimeError):
    """唯一进行中赛季已经超过允许修改高影响配置的时间窗口。"""


# 按当前运行时区计算赛季开始时刻，并在截止时刻前保持配置窗口开放。
def is_active_season_configuration_window_open(
    start_date: date,
    edit_window_hours: int,
    current_time: datetime | None = None,
) -> bool:
    effective_current_time = current_time or datetime.now().astimezone()
    if effective_current_time.tzinfo is None:
        effective_current_time = effective_current_time.astimezone()
    season_start_time = datetime.combine(
        start_date,
        time.min,
        tzinfo=effective_current_time.tzinfo,
    )
    deadline = season_start_time + timedelta(hours=edit_window_hours)
    return effective_current_time < deadline


# 在业务写事务内校验唯一激活赛季；无激活赛季时允许预先维护配置。
async def ensure_active_season_configuration_editable(
    session: AsyncSession,
    edit_window_hours: int,
    current_time: datetime | None = None,
) -> None:
    active_seasons = await lock_active_seasons_for_configuration(session)
    if len(active_seasons) > 1:
        raise MultipleActiveSeasonsForConfigurationError
    if not active_seasons:
        return
    if not is_active_season_configuration_window_open(
        active_seasons[0].start_date,
        edit_window_hours,
        current_time,
    ):
        raise ActiveSeasonConfigurationWindowClosedError
