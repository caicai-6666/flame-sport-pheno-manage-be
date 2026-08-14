"""定义用户意见查询与处理接口的数据结构。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PendingUserSuggestionResponse(BaseModel):
    id: int
    user_name: str
    avatar_url: str | None
    content: str
    created_at: datetime


class SuggestionProcessingRequest(BaseModel):
    suggestion_id: int = Field(gt=0)
    action: Literal["rejected", "resolved"]


class SuggestionProcessingResponse(BaseModel):
    suggestion_id: int
    processing_stage: Literal["rejected", "resolved"]
