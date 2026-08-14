"""集中承载赛季查询、配置、状态管理与结算操作接口。"""

from fastapi import APIRouter, HTTPException, status

from app.db.session import DatabaseSession
from app.schemas.season import (
    CreatedSeasonResponse,
    CreateSeasonRequest,
    SeasonListItemResponse,
)
from app.services.seasons import (
    InsufficientVisibleProjectsError,
    InvalidSeasonDateRangeError,
    SeasonStartDateConflictError,
    UnknownSeasonStatusError,
    create_season as create_season_service,
    list_seasons as list_seasons_service,
)

router = APIRouter(prefix="/season", tags=["season"])


# 接收全部赛季列表查询，并将起止日期序列化为稳定的管理端响应。
@router.get(
    "/list",
    response_model=list[SeasonListItemResponse],
    summary="获取全部赛季列表",
    responses={
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "赛季状态数据异常"
        },
    },
)
async def get_season_list(
    session: DatabaseSession,
) -> list[SeasonListItemResponse]:
    try:
        seasons = await list_seasons_service(session)
    except UnknownSeasonStatusError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="赛季状态数据异常",
        ) from error
    return [
        SeasonListItemResponse.model_validate(season, from_attributes=True)
        for season in seasons
    ]


# 接收赛季配置，并将日期冲突、周期不足和项目容量不足转换为明确响应。
@router.post(
    "/create",
    response_model=CreatedSeasonResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新增赛季",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "开始日期冲突或要求项目数超过当前可见项目数"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "请求字段非法或赛季周期不足一个完整日历月"
        },
    },
)
async def create_season(
    session: DatabaseSession,
    request: CreateSeasonRequest,
) -> CreatedSeasonResponse:
    try:
        season = await create_season_service(
            session,
            request.name,
            request.start_date,
            request.end_date,
            request.required_project_count,
        )
    except InvalidSeasonDateRangeError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="赛季周期不能少于一个完整日历月",
        ) from error
    except SeasonStartDateConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="赛季开始日期必须晚于已有赛季的最晚结束日期",
        ) from error
    except InsufficientVisibleProjectsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="要求项目个数不能超过当前可见项目个数",
        ) from error
    return CreatedSeasonResponse.model_validate(
        season,
        from_attributes=True,
    )
