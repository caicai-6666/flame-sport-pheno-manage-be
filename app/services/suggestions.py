"""编排管理端用户意见查询与处理用例。"""

from dataclasses import dataclass
from typing import Literal

from sqlmodel.ext.asyncio.session import AsyncSession

from app.repositories.suggestions import (
    PendingUserSuggestion,
    fetch_suggestion_for_processing,
    fetch_pending_user_suggestions,
    update_suggestion_processing_stage,
)

SuggestionProcessingAction = Literal["rejected", "resolved"]
PROCESSING_STAGE_BY_ACTION: dict[SuggestionProcessingAction, str] = {
    "rejected": "rejected",
    "resolved": "optimized",
}


class SuggestionNotFoundError(RuntimeError):
    """意见不存在或已经隐藏，不能通过管理端可见意见流程处理。"""


class SuggestionProcessingConflictError(RuntimeError):
    """意见已有不同处理结论，不能用另一个动作覆盖。"""


@dataclass(frozen=True, slots=True)
class SuggestionProcessingResult:
    suggestion_id: int
    processing_stage: SuggestionProcessingAction


# 在显式只读事务中拉取可见且待处理的意见，避免已处理记录重复进入工作列表。
async def list_pending_user_suggestions(
    session: AsyncSession,
) -> tuple[PendingUserSuggestion, ...]:
    async with session.begin():
        return await fetch_pending_user_suggestions(session)


# 在单一事务中锁定并处理意见；相同动作允许幂等重试，不同结论禁止覆盖。
async def process_user_suggestion(
    session: AsyncSession,
    suggestion_id: int,
    action: SuggestionProcessingAction,
) -> SuggestionProcessingResult:
    target_stage = PROCESSING_STAGE_BY_ACTION[action]
    async with session.begin():
        suggestion = await fetch_suggestion_for_processing(
            session,
            suggestion_id,
        )
        if suggestion is None or suggestion.status != 1:
            raise SuggestionNotFoundError
        if suggestion.processing_stage == target_stage:
            return SuggestionProcessingResult(
                suggestion_id=suggestion_id,
                processing_stage=action,
            )
        if suggestion.processing_stage != "pending":
            raise SuggestionProcessingConflictError
        await update_suggestion_processing_stage(
            session,
            suggestion_id,
            target_stage,
        )
    return SuggestionProcessingResult(
        suggestion_id=suggestion_id,
        processing_stage=action,
    )
