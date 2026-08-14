"""提供管理端用户基础信息查询接口。"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.db.session import DatabaseSession
from app.schemas.user import UserBasicInformationResponse, UserId
from app.services.users import get_user_basic_information

router = APIRouter(prefix="/user", tags=["user"])


# 接收批量用户查询参数，并把服务结果序列化为基础信息响应列表。
@router.get(
    "/user-info",
    response_model=list[UserBasicInformationResponse],
    summary="批量获取用户基础信息",
)
async def get_user_information(
    session: DatabaseSession,
    user_ids: Annotated[
        list[UserId],
        Query(
            min_length=1,
            max_length=50,
            description="用户 ID 列表，使用重复的 user_ids 查询参数传递",
        ),
    ],
) -> list[UserBasicInformationResponse]:
    users = await get_user_basic_information(session, user_ids)
    return [
        UserBasicInformationResponse.model_validate(user, from_attributes=True)
        for user in users
    ]
