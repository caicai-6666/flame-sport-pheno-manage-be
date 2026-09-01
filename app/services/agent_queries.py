"""为管理 API 编排查询智能体会话操作和安全响应转换。"""

from app.agent.text2sql.history_projection import (
    build_result_response,
    build_session_response,
    build_trace_response,
)
from app.agent.text2sql.interaction.session import AgentQuerySession
from app.agent.text2sql.query_manager import (
    AgentQueryManager,
    AgentQueryNotFoundError,
)
from app.schemas.agent_query import (
    AgentQueryCachedRecordIdsResponse,
    AgentQueryResultResponse,
    AgentQuerySessionResponse,
    AgentQueryTraceResponse,
)


# 构造查询会话安全快照，任何模型原始响应、工具参数和 SQL 都不会进入普通状态接口。
def build_agent_query_session_response(
    session: AgentQuerySession,
) -> AgentQuerySessionResponse:
    return build_session_response(session)


# 创建后台查询任务并立即返回会话状态，模型处理和交互等待不占用创建请求。
async def create_agent_query(
    manager: AgentQueryManager,
    question: str,
    domain_key: str,
) -> AgentQuerySessionResponse:
    session = await manager.start_query(question, domain_key)
    return build_agent_query_session_response(session)


# 优先读取内存会话；内存已释放时按标识回退加载持久化成功状态。
async def get_agent_query(
    manager: AgentQueryManager,
    query_id: str,
) -> AgentQuerySessionResponse:
    try:
        session = await manager.get_session(query_id)
    except AgentQueryNotFoundError as error:
        persisted = await manager.load_persisted_session(query_id)
        if persisted is not None:
            return persisted
        raise error
    return build_agent_query_session_response(session)


# 合并内存会话与持久化成功记录标识，调用方再按标识加载所需内容。
async def list_cached_agent_query_ids(
    manager: AgentQueryManager,
    limit: int,
) -> AgentQueryCachedRecordIdsResponse:
    return AgentQueryCachedRecordIdsResponse(
        query_ids=await manager.list_cached_query_ids(limit)
    )


# 原子提交指定交互答案并返回恢复后的最新查询快照。
async def answer_agent_query_interaction(
    manager: AgentQueryManager,
    query_id: str,
    interaction_id: str,
    answer: str,
) -> AgentQuerySessionResponse:
    session = await manager.answer_interaction(query_id, interaction_id, answer)
    return build_agent_query_session_response(session)


# 取消活动查询；终态查询重复取消保持原状态，不触发第二次事件。
async def cancel_agent_query(
    manager: AgentQueryManager,
    query_id: str,
) -> AgentQuerySessionResponse:
    session = await manager.cancel_query(query_id)
    return build_agent_query_session_response(session)


# 优先转换内存结果；会话过期后只按需加载持久化成功表格。
async def get_agent_query_result(
    manager: AgentQueryManager,
    query_id: str,
) -> AgentQueryResultResponse:
    try:
        session = await manager.get_session(query_id)
    except AgentQueryNotFoundError as error:
        persisted = await manager.load_persisted_result(query_id)
        if persisted is not None:
            return persisted
        raise error
    return build_result_response(session, query_id)


# 优先构造内存友好轨迹；会话过期后按需加载持久化成功轨迹。
async def get_agent_query_trace(
    manager: AgentQueryManager,
    query_id: str,
) -> AgentQueryTraceResponse:
    try:
        session = await manager.get_session(query_id)
    except AgentQueryNotFoundError as error:
        persisted = await manager.load_persisted_trace(query_id)
        if persisted is not None:
            return persisted
        raise error
    return build_trace_response(session)
