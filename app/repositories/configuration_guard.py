"""封装高影响业务配置变更所需的激活赛季锁定查询。"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass(frozen=True, slots=True)
class ActiveSeasonConfigurationReference:
    id: int
    start_date: date


# 共享锁定全部进行中赛季，为配置窗口判断提供同一事务内的一致快照。
async def lock_active_seasons_for_configuration(
    session: AsyncSession,
) -> tuple[ActiveSeasonConfigurationReference, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                season.id,
                season.start_date
            FROM season
            WHERE season.status = 1
            ORDER BY season.id ASC
            FOR SHARE
            """
        )
    )
    return tuple(
        ActiveSeasonConfigurationReference(
            id=int(row["id"]),
            start_date=row["start_date"],
        )
        for row in result.mappings().all()
    )
