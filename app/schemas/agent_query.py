"""定义查询智能体会话、交互、结果和 SSE 入口的 HTTP Schema。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentQueryCreateRequest(BaseModel):
    """创建一次指定业务域数据查询的请求。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    domain_key: str = Field(default="sports", min_length=1, max_length=64)


class AgentInteractionAnswerRequest(BaseModel):
    """恢复暂停查询时提交的单次用户回答。"""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=1000)


class AgentInteractionResponse(BaseModel):
    """前端需要展示的当前待回答交互。"""

    interaction_id: str
    interaction_type: Literal["confirmation", "clarification"]
    question: str
    options: list[str]
    allow_free_text: bool


class AgentQuerySessionResponse(BaseModel):
    """查询任务当前状态及待处理交互，不包含内部模型轨迹。"""

    query_id: str
    domain_key: str
    question: str
    status: str
    latest_sequence: int
    pending_interaction: AgentInteractionResponse | None
    result_available: bool
    user_message: str | None
    created_at: datetime
    updated_at: datetime


class AgentQueryCachedRecordIdsResponse(BaseModel):
    """当前进程内仍可按查询标识读取的缓存记录列表。"""

    query_ids: list[str] = Field(default_factory=list)


class AgentQueryResultHeaderResponse(BaseModel):
    """查询结果表的一列及其人类可读标题。"""

    key: str
    label: str


class AgentQueryResultResponse(BaseModel):
    """查询终态和供管理前端直接渲染的表格、统计及简洁审计说明。"""

    query_id: str
    status: str
    user_message: str | None
    matches_user_request: bool | None = None
    relevance_explanation: str | None = None
    table_description: str | None = None
    result_summary: str | None = None
    issues: list[str] = Field(default_factory=list)
    headers: list[AgentQueryResultHeaderResponse] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    statistics: dict[str, Any] | None = None


class AgentQueryTraceEntryResponse(BaseModel):
    """查询历史时间线中的一条面向操作员展示的安全记录。"""

    sequence: int
    entry_type: str
    stage: str
    status: str
    title: str
    message: str
    options: list[str]
    occurred_at: datetime


class AgentQueryTraceResponse(BaseModel):
    """按查询标识返回的内存友好轨迹，不包含表格、SQL 或模型原始输出。"""

    query_id: str
    domain_key: str
    question: str
    aligned_question: str | None = None
    status: str
    user_message: str | None
    created_at: datetime
    updated_at: datetime
    entries: list[AgentQueryTraceEntryResponse] = Field(default_factory=list)
