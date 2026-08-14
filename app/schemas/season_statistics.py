"""定义赛季统计查询接口的响应结构。"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class CurrentSeasonParticipantResponse(BaseModel):
    season_user_id: int
    user_id: str
    level_id: int
    level_name: str


class CurrentSeasonResponse(BaseModel):
    id: int
    name: str
    start_date: date
    end_date: date
    required_project_count: int
    status: Literal[1]
    participants: list[CurrentSeasonParticipantResponse]


class CurrentSeasonProjectParticipantResponse(BaseModel):
    user_id: str
    completion_progress: float = Field(ge=0, le=1)
