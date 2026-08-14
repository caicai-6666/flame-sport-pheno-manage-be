"""封装管理端挑战等级的数据库查询与写入。"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass(frozen=True, slots=True)
class ProjectLevelInformation:
    id: int
    name: str
    reward: int


# 查询全部挑战等级，不按启停状态过滤，并按奖励积分与主键稳定排序。
async def fetch_all_project_levels(
    session: AsyncSession,
) -> tuple[ProjectLevelInformation, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                project_level.id,
                project_level.name,
                project_level.reward
            FROM project_level
            ORDER BY project_level.reward ASC, project_level.id ASC
            """
        )
    )
    return tuple(
        ProjectLevelInformation(
            id=int(row["id"]),
            name=str(row["name"]),
            reward=int(row["reward"]),
        )
        for row in result.mappings().all()
    )


# 写入默认启用的新挑战等级，并返回数据库生成的主键及请求字段。
async def insert_project_level(
    session: AsyncSession,
    name: str,
    reward: int,
) -> ProjectLevelInformation:
    result = await session.exec(
        text(
            """
            INSERT INTO project_level (
                name,
                reward,
                status
            ) VALUES (
                :name,
                :reward,
                1
            )
            """
        ),
        params={
            "name": name,
            "reward": reward,
        },
    )
    return ProjectLevelInformation(
        id=int(result.lastrowid),
        name=name,
        reward=reward,
    )


# 锁定目标等级后覆盖奖励积分；等级不存在时不执行更新并返回空结果。
async def update_project_level_reward(
    session: AsyncSession,
    level_id: int,
    reward: int,
) -> ProjectLevelInformation | None:
    result = await session.exec(
        text(
            """
            SELECT
                project_level.id,
                project_level.name
            FROM project_level
            WHERE project_level.id = :level_id
            FOR UPDATE
            """
        ),
        params={"level_id": level_id},
    )
    row = result.mappings().first()
    if row is None:
        return None

    await session.exec(
        text(
            """
            UPDATE project_level
            SET reward = :reward
            WHERE id = :level_id
            """
        ),
        params={
            "level_id": level_id,
            "reward": reward,
        },
    )
    return ProjectLevelInformation(
        id=int(row["id"]),
        name=str(row["name"]),
        reward=reward,
    )
