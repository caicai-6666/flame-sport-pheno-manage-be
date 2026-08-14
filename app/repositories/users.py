"""封装管理端用户基础信息的只读数据库查询。"""

from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass(frozen=True, slots=True)
class UserBasicInformation:
    user_id: str
    name: str
    department_name: str
    avatar_url: str | None


# 批量查询用户及其部门展示信息，并按调用方首次传入的用户 ID 顺序返回结果。
async def fetch_user_basic_information(
    session: AsyncSession,
    user_ids: tuple[str, ...],
) -> tuple[UserBasicInformation, ...]:
    if not user_ids:
        return ()

    statement = text(
        """
        SELECT
            user_account.id AS user_id,
            user_account.name,
            department.name AS department_name,
            user_account.avatar_url
        FROM `user` AS user_account
        JOIN department
            ON department.id = user_account.department_id
        WHERE user_account.id IN :user_ids
        """
    ).bindparams(bindparam("user_ids", expanding=True))
    result = await session.exec(statement, params={"user_ids": user_ids})
    users_by_id = {
        str(row["user_id"]): UserBasicInformation(
            user_id=str(row["user_id"]),
            name=str(row["name"]),
            department_name=str(row["department_name"]),
            avatar_url=(
                str(row["avatar_url"])
                if row["avatar_url"] is not None
                else None
            ),
        )
        for row in result.mappings().all()
    }
    return tuple(
        users_by_id[user_id]
        for user_id in user_ids
        if user_id in users_by_id
    )
