"""封装管理端用户意见查询、行锁读取与处理阶段写入。"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass(frozen=True, slots=True)
class PendingUserSuggestion:
    id: int
    user_name: str
    avatar_url: str | None
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SuggestionForProcessing:
    id: int
    status: int
    processing_stage: str


# 查询可见且待处理的意见与用户展示信息，并按创建时间和主键倒序稳定返回。
async def fetch_pending_user_suggestions(
    session: AsyncSession,
) -> tuple[PendingUserSuggestion, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                user_suggestion.id,
                user_account.name AS user_name,
                user_account.avatar_url,
                user_suggestion.content,
                user_suggestion.created_at
            FROM user_suggestion
            JOIN `user` AS user_account
                ON user_account.id = user_suggestion.user_id
            WHERE user_suggestion.status = 1
              AND user_suggestion.processing_stage = 'pending'
            ORDER BY
                user_suggestion.created_at DESC,
                user_suggestion.id DESC
            """
        )
    )
    return tuple(
        PendingUserSuggestion(
            id=int(row["id"]),
            user_name=str(row["user_name"]),
            avatar_url=(
                str(row["avatar_url"])
                if row["avatar_url"] is not None
                else None
            ),
            content=str(row["content"]),
            created_at=row["created_at"],
        )
        for row in result.mappings().all()
    )


# 锁定指定意见并读取可见状态与处理阶段，防止并发操作覆盖彼此结论。
async def fetch_suggestion_for_processing(
    session: AsyncSession,
    suggestion_id: int,
) -> SuggestionForProcessing | None:
    result = await session.exec(
        text(
            """
            SELECT
                user_suggestion.id,
                user_suggestion.status,
                user_suggestion.processing_stage
            FROM user_suggestion
            WHERE user_suggestion.id = :suggestion_id
            FOR UPDATE
            """
        ),
        params={"suggestion_id": suggestion_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return SuggestionForProcessing(
        id=int(row["id"]),
        status=int(row["status"]),
        processing_stage=str(row["processing_stage"]),
    )


# 在持有意见行锁时写入最终处理阶段，更新值只能来自服务层固定动作映射。
async def update_suggestion_processing_stage(
    session: AsyncSession,
    suggestion_id: int,
    processing_stage: str,
) -> None:
    await session.exec(
        text(
            """
            UPDATE user_suggestion
            SET processing_stage = :processing_stage
            WHERE id = :suggestion_id
            """
        ),
        params={
            "suggestion_id": suggestion_id,
            "processing_stage": processing_stage,
        },
    )
