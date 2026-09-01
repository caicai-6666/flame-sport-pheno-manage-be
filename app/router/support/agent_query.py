"""提供查询智能体路由的依赖、异常与 SSE 传输辅助能力。"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.agent.text2sql.events.sse import encode_sse_event, encode_sse_heartbeat
from app.agent.text2sql.query_manager import (
    AgentQueryCapacityError,
    AgentQueryManager,
    AgentQueryNotFoundError,
)


# 从应用生命周期读取共享查询管理器，保证状态接口、SSE 和回答接口访问同一批会话。
def get_agent_query_manager(request: Request) -> AgentQueryManager:
    return request.app.state.agent_query_manager


AgentQueryManagerDependency = Annotated[
    AgentQueryManager,
    Depends(get_agent_query_manager),
]


# 将查询会话常见业务异常映射为稳定 HTTP 状态，避免路由泄漏内部技术错误。
def raise_agent_query_http_error(error: Exception) -> None:
    if isinstance(error, AgentQueryNotFoundError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, AgentQueryCapacityError):
        raise HTTPException(status_code=429, detail=str(error)) from error
    if isinstance(error, KeyError):
        raise HTTPException(status_code=422, detail=str(error).strip("'")) from error
    if isinstance(error, ValueError):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, RuntimeError):
        raise HTTPException(status_code=503, detail=str(error)) from error
    raise error


# 将 Last-Event-ID 安全转换为非负序号，格式错误明确返回 400 而不是静默漏发事件。
def parse_last_event_id(last_event_id: str | None) -> int:
    if last_event_id is None or not last_event_id.strip():
        return 0
    try:
        sequence = int(last_event_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Last-Event-ID 必须是非负整数") from error
    if sequence < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID 必须是非负整数")
    return sequence


# 订阅历史补发和实时事件；客户端断开时及时结束生成器并释放订阅队列。
async def stream_agent_query_events(
    request: Request,
    manager: AgentQueryManager,
    query_id: str,
    after_sequence: int,
) -> AsyncIterator[str]:
    async for event in manager.subscribe(query_id, after_sequence):
        if await request.is_disconnected():
            return
        yield encode_sse_heartbeat() if event is None else encode_sse_event(event)
