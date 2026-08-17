"""统一执行激活赛季期间高影响业务配置的时间窗口校验。"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlmodel.ext.asyncio.session import AsyncSession

from app.repositories.configuration_guard import (
    lock_active_seasons_for_configuration,
)

APPLICATION_TIMEZONE = ZoneInfo("Asia/Shanghai")


class MultipleActiveSeasonsForConfigurationError(RuntimeError):
    """存在多个进行中赛季，无法确定唯一配置窗口。"""


class ActiveSeasonConfigurationWindowClosedError(RuntimeError):
    """唯一进行中赛季已经超过允许修改高影响配置的时间窗口。"""


# 统一按上海时区计算赛季窗口，避免容器系统时区变化造成八小时偏差。
def is_active_season_configuration_window_open(
    start_date: date,
    edit_window_hours: int,
    current_time: datetime | None = None,
) -> bool:
    effective_current_time = current_time or datetime.now(APPLICATION_TIMEZONE)
    if effective_current_time.tzinfo is None:
        effective_current_time = effective_current_time.replace(
            tzinfo=APPLICATION_TIMEZONE
        )
    else:
        effective_current_time = effective_current_time.astimezone(
            APPLICATION_TIMEZONE
        )
    season_start_time = datetime.combine(
        start_date,
        time.min,
        tzinfo=APPLICATION_TIMEZONE,
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
