"""定义可暂停查询中的用户交互和会话状态。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.events.models import now_in_shanghai


AgentInteractionType = Literal["confirmation", "clarification"]
AgentInteractionStatus = Literal["pending", "answered", "cancelled"]
AgentQueryStatus = Literal[
    "running",
    "waiting_for_confirmation",
    "waiting_for_clarification",
    "completed",
    "abandoned",
    "failed",
    "cancelled",
]


class AgentInteraction(BaseModel):
    """一次需要前端展示并由用户回答的查询交互。"""

    model_config = ConfigDict(extra="forbid")

    interaction_id: str
    interaction_type: AgentInteractionType
    question: str = Field(min_length=1, max_length=1000)
    options: tuple[str, ...] = ()
    allow_free_text: bool
    status: AgentInteractionStatus = "pending"
    answer: str | None = None
    created_at: datetime = Field(default_factory=now_in_shanghai)
    answered_at: datetime | None = None


class AgentQuerySessionSnapshot(BaseModel):
    """供服务和路由读取的查询会话安全快照，不包含模型原始响应与内部 SQL。"""

    model_config = ConfigDict(extra="forbid")

    query_id: str
    domain_key: str
    question: str
    status: AgentQueryStatus
    latest_sequence: int
    pending_interaction: AgentInteraction | None = None
    result_available: bool
    user_message: str | None = None
    created_at: datetime
    updated_at: datetime
