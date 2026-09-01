"""把内存查询会话投影为前端协议允许持久化的安全响应。"""

from app.agent.text2sql.interaction.session import AgentQuerySession
from app.schemas.agent_query import (
    AgentInteractionResponse,
    AgentQueryResultHeaderResponse,
    AgentQueryResultResponse,
    AgentQuerySessionResponse,
    AgentQueryTraceEntryResponse,
    AgentQueryTraceResponse,
)


# 将当前待回答交互转换为前端字段；已回答内容只通过友好轨迹展示。
def build_interaction_response(
    session: AgentQuerySession,
) -> AgentInteractionResponse | None:
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


# 构造不包含内部模型轨迹、工具参数和 SQL 的会话状态响应。
def build_session_response(
    session: AgentQuerySession,
) -> AgentQuerySessionResponse:
    snapshot = session.snapshot()
    return AgentQuerySessionResponse(
        query_id=snapshot.query_id,
        domain_key=snapshot.domain_key,
        question=snapshot.question,
        status=snapshot.status,
        latest_sequence=snapshot.latest_sequence,
        pending_interaction=build_interaction_response(session),
        result_available=snapshot.result_available,
        user_message=snapshot.user_message,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


# 从安全终态结果构造前端表格、统计和审计说明，失败元数据保持现有协议。
def build_result_response(
    session: AgentQuerySession,
    query_id: str | None = None,
) -> AgentQueryResultResponse:
    snapshot = session.snapshot()
    result = session.get_result()
    response = AgentQueryResultResponse(
        query_id=query_id or snapshot.query_id,
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


# 构造前端友好时间线；只暴露对齐问题和已裁剪的关键过程记录。
def build_trace_response(
    session: AgentQuerySession,
) -> AgentQueryTraceResponse:
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
