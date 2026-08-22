"""提供查询创建、进度流、交互恢复、结果读取和取消接口。"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.agent.events.sse import encode_sse_event, encode_sse_heartbeat
from app.agent.query_manager import (
    AgentQueryCapacityError,
    AgentQueryManager,
    AgentQueryNotFoundError,
)
from app.schemas.agent_query import (
    AgentInteractionAnswerRequest,
    AgentQueryCachedRecordIdsResponse,
    AgentQueryCreateRequest,
    AgentQueryResultResponse,
    AgentQuerySessionResponse,
    AgentQueryTraceResponse,
)
from app.services.agent_queries import (
    answer_agent_query_interaction,
    cancel_agent_query,
    create_agent_query,
    get_agent_query,
    get_agent_query_result,
    get_agent_query_trace as get_agent_query_trace_service,
    list_cached_agent_query_ids,
)


router = APIRouter(prefix="/agent/queries", tags=["agent-query"])


# 从应用生命周期读取共享查询管理器，保证状态接口、SSE 和回答接口访问同一批会话。
def get_agent_query_manager(request: Request) -> AgentQueryManager:
    return request.app.state.agent_query_manager


AgentQueryManagerDependency = Annotated[
    AgentQueryManager,
    Depends(get_agent_query_manager),
]


# 将查询会话常见业务异常映射为稳定 HTTP 状态，避免路由泄漏内部技术错误。
def _raise_agent_query_http_error(error: Exception) -> None:
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


# 创建后台查询并返回 202，会话后续进展通过独立 SSE 连接实时发送。
@router.post(
    "",
    response_model=AgentQuerySessionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_query(
    payload: AgentQueryCreateRequest,
    manager: AgentQueryManagerDependency,
) -> AgentQuerySessionResponse:
    try:
        return await create_agent_query(
            manager,
            payload.question,
            payload.domain_key,
        )
    except Exception as error:
        _raise_agent_query_http_error(error)
        raise


# 列出当前进程保留期内的查询标识；前端按标识调用轨迹或结果接口，不在列表中批量传输历史内容。
@router.get("/cached-record-ids", response_model=AgentQueryCachedRecordIdsResponse)
async def get_cached_record_ids(
    manager: AgentQueryManagerDependency,
    limit: int = Query(default=100, ge=1, le=200),
) -> AgentQueryCachedRecordIdsResponse:
    try:
        return await list_cached_agent_query_ids(manager, limit)
    except Exception as error:
        _raise_agent_query_http_error(error)
        raise


# 查询当前任务、待回答交互和结果可用状态，不返回技术轨迹或完整查询结果。
@router.get("/{query_id}", response_model=AgentQuerySessionResponse)
async def get_query(
    query_id: str,
    manager: AgentQueryManagerDependency,
) -> AgentQuerySessionResponse:
    try:
        return await get_agent_query(manager, query_id)
    except Exception as error:
        _raise_agent_query_http_error(error)
        raise


# 返回当前内存保留期内的友好交互与关键阶段轨迹，表格结果仍由独立 result 接口读取。
@router.get("/{query_id}/trace", response_model=AgentQueryTraceResponse)
async def get_query_trace(
    query_id: str,
    manager: AgentQueryManagerDependency,
) -> AgentQueryTraceResponse:
    try:
        return await get_agent_query_trace_service(manager, query_id)
    except Exception as error:
        _raise_agent_query_http_error(error)
        raise


# 将 Last-Event-ID 安全转换为非负序号，格式错误明确返回 400 而不是静默漏发事件。
def _parse_last_event_id(last_event_id: str | None) -> int:
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
async def _stream_query_events(
    request: Request,
    manager: AgentQueryManager,
    query_id: str,
    after_sequence: int,
) -> AsyncIterator[str]:
    async for event in manager.subscribe(query_id, after_sequence):
        if await request.is_disconnected():
            return
        yield encode_sse_heartbeat() if event is None else encode_sse_event(event)


# 建立只由服务器发送事件的长连接，关闭代理缓冲以保证关键进度及时到达页面。
@router.get("/{query_id}/events", response_class=StreamingResponse)
async def stream_query_events(
    request: Request,
    query_id: str,
    manager: AgentQueryManagerDependency,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    after_sequence = _parse_last_event_id(last_event_id)
    try:
        await manager.get_session(query_id)
    except Exception as error:
        _raise_agent_query_http_error(error)
        raise
    return StreamingResponse(
        _stream_query_events(request, manager, query_id, after_sequence),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# 提交一次待回答交互并唤醒后台工作线程，错误交互 ID 和重复回答按冲突处理。
@router.post(
    "/{query_id}/interactions/{interaction_id}/answer",
    response_model=AgentQuerySessionResponse,
)
async def answer_query_interaction(
    query_id: str,
    interaction_id: str,
    payload: AgentInteractionAnswerRequest,
    manager: AgentQueryManagerDependency,
) -> AgentQuerySessionResponse:
    try:
        return await answer_agent_query_interaction(
            manager,
            query_id,
            interaction_id,
            payload.answer,
        )
    except Exception as error:
        _raise_agent_query_http_error(error)
        raise


# 返回当前终态结果；运行中查询返回空表和当前状态，前端可继续监听 SSE。
@router.get("/{query_id}/result", response_model=AgentQueryResultResponse)
async def get_query_result(
    query_id: str,
    response: Response,
    manager: AgentQueryManagerDependency,
) -> AgentQueryResultResponse:
    try:
        result = await get_agent_query_result(manager, query_id)
    except Exception as error:
        _raise_agent_query_http_error(error)
        raise
    if result.status in {
        "running",
        "waiting_for_confirmation",
        "waiting_for_clarification",
    }:
        response.status_code = status.HTTP_202_ACCEPTED
    return result


# 取消运行中或等待交互的查询，已结束任务保持原状态并按幂等成功返回。
@router.delete("/{query_id}", response_model=AgentQuerySessionResponse)
async def delete_query(
    query_id: str,
    manager: AgentQueryManagerDependency,
) -> AgentQuerySessionResponse:
    try:
        return await cancel_agent_query(manager, query_id)
    except Exception as error:
        _raise_agent_query_http_error(error)
        raise
