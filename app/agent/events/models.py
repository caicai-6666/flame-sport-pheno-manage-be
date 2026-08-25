"""定义查询智能体面向前端的结构化进度事件。"""

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field


AgentQueryStage = Literal[
    "accepted",
    "alignment",
    "confirmation",
    "planning",
    "sql_generation",
    "execution",
    "translation",
    "shaping",
    "result",
]
AgentEventType = Literal[
    "query_started",
    "stage_started",
    "progress_updated",
    "interaction_required",
    "stage_completed",
    "query_completed",
    "query_abandoned",
    "query_cancelled",
    "query_failed",
    "heartbeat",
]
AgentEventStatus = Literal[
    "running",
    "waiting",
    "success",
    "abandoned",
    "cancelled",
    "failure",
]
AgentQueryTraceEntryType = Literal[
    "question_submitted",
    "progress",
    "interaction_requested",
    "interaction_answered",
]


# 统一生成上海时区事件时间，避免依赖容器或宿主机默认时区。
def now_in_shanghai() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


class AgentProgressUpdate(BaseModel):
    """智能体内部发布的无序号进度更新，由查询会话补齐标识和顺序。"""

    model_config = ConfigDict(extra="forbid")

    stage: AgentQueryStage
    event_type: AgentEventType
    status: AgentEventStatus
    title: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentProgressEvent(AgentProgressUpdate):
    """可通过 SSE 推送、支持断线补发的完整查询进度事件。"""

    query_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    occurred_at: datetime = Field(default_factory=now_in_shanghai)


class AgentQueryTraceEntry(BaseModel):
    """供查询历史页面展示的一条安全、友好的关键过程记录。"""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    entry_type: AgentQueryTraceEntryType
    stage: AgentQueryStage
    status: AgentEventStatus
    title: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(default_factory=list)
    occurred_at: datetime = Field(default_factory=now_in_shanghai)
