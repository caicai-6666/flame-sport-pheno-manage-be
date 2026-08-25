"""为管理 API 编排查询智能体会话操作和安全响应转换。"""

from app.agent.interaction.session import AgentQuerySession
from app.agent.query_manager import AgentQueryManager
from app.schemas.agent_query import (
    AgentInteractionResponse,
    AgentQueryCachedRecordIdsResponse,
    AgentQueryResultHeaderResponse,
    AgentQueryResultResponse,
    AgentQuerySessionResponse,
    AgentQueryTraceEntryResponse,
    AgentQueryTraceResponse,
)


# 将内部交互模型转换为前端所需字段，隐藏回答时间和线程等待状态。
def _build_interaction_response(session: AgentQuerySession) -> AgentInteractionResponse | None:
    interaction = session.snapshot().pending_interaction
    if interaction is None or interaction.status != "pending":
        return None
    return AgentInteractionResponse(
        interaction_id=interaction.interaction_id,
        interaction_type=interaction.interaction_type,
        question=interaction.question,
        options=list(interaction.options),
        allow_free_text=interaction.allow_free_text,
    )


# 构造查询会话安全快照，任何模型原始响应、工具参数和 SQL 都不会进入普通状态接口。
def build_agent_query_session_response(
    session: AgentQuerySession,
) -> AgentQuerySessionResponse:
    snapshot = session.snapshot()
    return AgentQuerySessionResponse(
        query_id=snapshot.query_id,
        domain_key=snapshot.domain_key,
        question=snapshot.question,
        status=snapshot.status,
        latest_sequence=snapshot.latest_sequence,
        pending_interaction=_build_interaction_response(session),
        result_available=snapshot.result_available,
        user_message=snapshot.user_message,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


# 创建后台查询任务并立即返回会话状态，模型处理和交互等待不占用创建请求。
async def create_agent_query(
    manager: AgentQueryManager,
    question: str,
    domain_key: str,
) -> AgentQuerySessionResponse:
    session = await manager.start_query(question, domain_key)
    return build_agent_query_session_response(session)


# 读取查询状态并清理可能已经过期的终态会话。
async def get_agent_query(
    manager: AgentQueryManager,
    query_id: str,
) -> AgentQuerySessionResponse:
    session = await manager.get_session(query_id)
    return build_agent_query_session_response(session)


# 列出保留期内的查询标识；调用方必须再使用单条接口读取对应轨迹或表格，避免一次返回全部历史内容。
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


# 将内部结果转换为表格、摘要或安全失败元数据，明确排除 SQL、数据结构与模型原始轨迹。
async def get_agent_query_result(
    manager: AgentQueryManager,
    query_id: str,
) -> AgentQueryResultResponse:
    session = await manager.get_session(query_id)
    snapshot = session.snapshot()
    result = session.get_result()
    response = AgentQueryResultResponse(
        query_id=query_id,
        status=snapshot.status,
        user_message=snapshot.user_message,
    )
    if result is None:
        return response
    if result.status == "failure" and result.sql_result is not None:
        response.failure_stage = "sql"
        response.failure_code = result.sql_result.error_code
        response.failure_retry_target = result.sql_result.retry_target
        response.failure_attempt_count = result.sql_result.generation_count
        response.failure_attempt_limit = result.sql_result.max_generation_count
    audit_result = result.audit_result
    if audit_result is None:
        return response
    response.headers = [
        AgentQueryResultHeaderResponse(key=header.key, label=header.label)
        for header in audit_result.display_result.headers
    ]
    response.rows = audit_result.display_result.rows
    response.statistics = audit_result.statistics.model_dump(mode="json")
    assessment = audit_result.assessment
    if assessment is not None:
        response.matches_user_request = assessment.matches_user_request
        response.relevance_explanation = assessment.relevance_explanation
        response.table_description = assessment.table_description
        response.result_summary = assessment.result_summary
        response.issues = assessment.issues
    return response


# 返回查询会话的安全友好轨迹；对齐问题只从已裁剪的终态结果读取，避免暴露模型原始轨迹。
async def get_agent_query_trace(
    manager: AgentQueryManager,
    query_id: str,
) -> AgentQueryTraceResponse:
    session = await manager.get_session(query_id)
    snapshot = session.snapshot()
    result = session.get_result()
    aligned_question: str | None = None
    if result is not None and result.alignment_result is not None:
        aligned_request = result.alignment_result.aligned_request
        if aligned_request is not None:
            aligned_question = aligned_request.aligned_question
    return AgentQueryTraceResponse(
        query_id=snapshot.query_id,
        domain_key=snapshot.domain_key,
        question=snapshot.question,
        aligned_question=aligned_question,
        status=snapshot.status,
        user_message=snapshot.user_message,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        entries=[
            AgentQueryTraceEntryResponse(**entry.model_dump())
            for entry in session.get_trace_entries()
        ],
    )
