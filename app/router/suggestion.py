"""提供管理端用户意见查询与处理接口。"""

from fastapi import APIRouter, HTTPException, status

from app.db.session import DatabaseSession
from app.schemas.suggestion import (
    PendingUserSuggestionResponse,
    SuggestionProcessingRequest,
    SuggestionProcessingResponse,
)
from app.services.suggestions import (
    SuggestionNotFoundError,
    SuggestionProcessingConflictError,
    list_pending_user_suggestions,
    process_user_suggestion,
)

router = APIRouter(prefix="/suggestion", tags=["suggestion"])


# 接收意见列表请求，只将可见且待处理的意见及用户信息序列化为稳定响应。
@router.get(
    "/list",
    response_model=list[PendingUserSuggestionResponse],
    summary="拉取可见且待处理的用户意见",
)
async def get_pending_user_suggestions(
    session: DatabaseSession,
) -> list[PendingUserSuggestionResponse]:
    suggestions = await list_pending_user_suggestions(session)
    return [
        PendingUserSuggestionResponse.model_validate(
            suggestion,
            from_attributes=True,
        )
        for suggestion in suggestions
    ]


# 接收意见 ID 与处理动作，并将不存在和结论冲突映射为明确的 HTTP 响应。
@router.post(
    "/process",
    response_model=SuggestionProcessingResponse,
    summary="处理用户意见",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "意见不存在或已隐藏"},
        status.HTTP_409_CONFLICT: {"description": "意见已有不同处理结论"},
    },
)
async def submit_suggestion_processing(
    session: DatabaseSession,
    request: SuggestionProcessingRequest,
) -> SuggestionProcessingResponse:
    try:
        result = await process_user_suggestion(
            session,
            request.suggestion_id,
            request.action,
        )
    except SuggestionNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="意见不存在或已隐藏",
        ) from error
    except SuggestionProcessingConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="意见已有不同处理结论，不能重复处理",
        ) from error
    return SuggestionProcessingResponse.model_validate(
        result,
        from_attributes=True,
    )
