"""定义赛季管理接口的请求与响应结构。"""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints


class SeasonListItemResponse(BaseModel):
    id: int
    name: str
    start_date: date
    end_date: date
    status: Literal[0, 1, 2, 3]
    status_name: Literal["未开始", "进行中", "结算中", "已结束"]


class CreateSeasonRequest(BaseModel):
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    ]
    start_date: date
    end_date: date
    required_project_count: int = Field(ge=1, le=255)


class CreatedSeasonResponse(SeasonListItemResponse):
    required_project_count: int
