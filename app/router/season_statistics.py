"""集中承载赛季维度的只读聚合查询接口。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.db.session import DatabaseSession
from app.schemas.season_statistics import (
    CurrentSeasonProjectParticipantResponse,
    CurrentSeasonResponse,
)
from app.services.season_statistics import (
    CurrentSeasonConflictError,
    CurrentSeasonNotFoundError,
    get_current_season_project_participants,
    get_current_season_statistics,
)

router = APIRouter(
    prefix="/season-statistics",
    tags=["season-statistics"],
)


# 接收当前赛季查询，并将服务层业务异常转换为明确的 HTTP 状态。
@router.get(
    "/current",
    response_model=CurrentSeasonResponse,
    summary="获取当前赛季基础统计信息",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "当前没有激活的赛季"},
        status.HTTP_409_CONFLICT: {"description": "存在多个激活赛季"},
    },
)
async def get_current_season_statistics_route(
    session: DatabaseSession,
) -> CurrentSeasonResponse:
    try:
        current_season = await get_current_season_statistics(session)
    except CurrentSeasonConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="存在多个激活赛季，无法确定当前赛季",
        ) from error
    except CurrentSeasonNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="当前没有激活的赛季",
        ) from error

    return CurrentSeasonResponse.model_validate(current_season, from_attributes=True)


# 接收赛季用户记录和项目标识，返回当前赛季中对应的有效参赛项目进度。
@router.get(
    "/project-participants",
    response_model=list[CurrentSeasonProjectParticipantResponse],
    summary="查询当前赛季项目参赛人员与进度",
)
async def get_project_participants(
    session: DatabaseSession,
    season_user_id: Annotated[
        int,
        Query(gt=0, description="当前赛季用户记录 ID"),
    ],
    project_id: Annotated[
        int,
        Query(gt=0, description="待查询的运动项目 ID"),
    ],
) -> list[CurrentSeasonProjectParticipantResponse]:
    participants = await get_current_season_project_participants(
        session,
        season_user_id,
        project_id,
    )
    return [
        CurrentSeasonProjectParticipantResponse.model_validate(
            participant,
            from_attributes=True,
        )
        for participant in participants
    ]
