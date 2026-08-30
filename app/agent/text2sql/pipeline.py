"""装配业务对齐、交互式查询规划、翻译与结果审计主链路。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.agent.text2sql.domains.base import QueryDomainProfile
from app.agent.text2sql.subgraphs.alignment import (
    BusinessAlignmentResult,
    BusinessAlignmentSubgraph,
)
from app.agent.text2sql.subgraphs.planning import (
    DeepSeekQueryPlanningAgent,
    QueryPlanningAgentResult,
)
from app.agent.text2sql.subgraphs.audit import (
    QueryResultAuditResult,
    QueryResultAuditSubgraph,
)
from app.agent.text2sql.subgraphs.shaping import ResultShapingSubgraphResult
from app.agent.text2sql.subgraphs.translation import (
    ResultTranslationSubgraph,
    ResultTranslationSubgraphResult,
)
from app.agent.text2sql.subgraphs.sql import SqlQuerySubgraphResult
from app.agent.text2sql.events.models import AgentProgressUpdate
from app.agent.text2sql.events.publisher import ProgressEmitter
from app.agent.text2sql.model_messages import ModelMessageTraceQueue
from app.agent.text2sql.subgraphs.planning.tools.table_schema_cache import CachingTableSchemaReader
from app.agent.text2sql.subgraphs.planning.tools.table_schema_reader import InformationSchemaTableSchemaReader
from app.agent.text2sql.subgraphs.planning.tools.table_schema import TableSchemaToolResponse
from app.core.config import Settings, get_settings


InteractionRequester = Callable[
    [Literal["confirmation", "clarification"], str, tuple[str, ...]],
    str,
]
TraceWriter = Callable[[str], None]
SchemaReader = Callable[[str], TableSchemaToolResponse]
UserMessageFormatter = Callable[[str], Awaitable[str]]


class AgentQueryPipelineResult(BaseModel):
    """一次完整查询流水线的结构化终态，技术轨迹不会直接作为 HTTP 响应暴露。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "abandoned", "failure"]
    domain_key: str
    user_question: str
    alignment_result: BusinessAlignmentResult | None = None
    planning_result: QueryPlanningAgentResult | None = None
    sql_result: SqlQuerySubgraphResult | None = None
    translation_result: ResultTranslationSubgraphResult | None = None
    shaping_result: ResultShapingSubgraphResult | None = None
    audit_result: QueryResultAuditResult | None = None
    user_message: str | None = None
    error: str | None = None


# 优先复用已通过约束审计的结果摘要作为终态文案，使 SSE、轨迹和查询状态展示相同的业务结论。
def _build_result_message(audit_result: QueryResultAuditResult) -> str:
    if audit_result.status == "success" and audit_result.assessment is not None:
        return audit_result.assessment.result_summary
    return "查询数据和表格已生成，结果说明暂时不可用。"


class AgentQueryPipeline:
    """以业务域配置装配主链路；SQL 与塑形由规划阶段的受控工具完成。"""

    # 保存不可变业务域、运行预算和交互出口，实际模型与数据库适配器在单次运行中装配。
    def __init__(
        self,
        domain_profile: QueryDomainProfile,
        interaction_requester: InteractionRequester,
        progress_emitter: ProgressEmitter | None = None,
        settings: Settings | None = None,
        trace_writer: TraceWriter | None = None,
        message_trace_queue: ModelMessageTraceQueue | None = None,
        schema_reader: SchemaReader | None = None,
        user_message_formatter: UserMessageFormatter | None = None,
    ) -> None:
        domain_profile.validate_resources()
        self._domain_profile = domain_profile
        self._interaction_requester = interaction_requester
        self._progress_emitter = progress_emitter
        self._settings = settings or get_settings()
        self._trace_writer = trace_writer
        self._message_trace_queue = message_trace_queue
        self._schema_reader = schema_reader
        self._user_message_formatter = user_message_formatter

    # 发布面向操作员的稳定阶段事件，禁止把 SQL、工具参数或原始模型文本塞入 message。
    def _emit(
        self,
        stage: str,
        event_type: str,
        status: str,
        title: str,
        message: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        if self._progress_emitter is None:
            return
        self._progress_emitter(
            AgentProgressUpdate(
                stage=stage,
                event_type=event_type,
                status=status,
                title=title,
                message=message,
                payload=payload or {},
            )
        )

    # 在唯一展示边界调用格式化器；模型或供应商异常时必须回退原文，不得阻断主查询。
    async def _format_user_message(self, message: str) -> str:
        if self._user_message_formatter is None:
            return message
        try:
            return await self._user_message_formatter(message)
        except Exception:
            return message

    # 将子图的单问题回调转换为可暂停的正式交互请求，答案由查询会话恢复后同步返回。
    def _request_clarification(self, question: str) -> str:
        return self._interaction_requester("clarification", question, ())

    # 先整理用户可见问题，再在线程中等待同步会话交互，避免阻塞 FastAPI 事件循环。
    async def _request_clarification_async(self, question: str) -> str:
        formatted_question = await self._format_user_message(question)
        return await asyncio.to_thread(
            self._request_clarification,
            formatted_question,
        )

    # 复核查询计划时只提供确认或修正选择，具体修正内容由后续独立澄清交互收集。
    def _request_plan_review(self, question: str) -> str:
        return self._interaction_requester(
            "confirmation",
            question,
            ("确认并继续", "修正查询"),
        )

    # 先整理规划复核问题，再在线程中等待答案，避免阻塞事件循环和 SSE 心跳。
    async def _request_plan_review_async(self, question: str) -> str:
        formatted_question = await self._format_user_message(question)
        return await asyncio.to_thread(
            self._request_plan_review,
            formatted_question,
        )

    # 展示业务对齐结果并提供确认或修正入口，具体修正内容由随后自由文本交互收集。
    def _request_alignment_review(self, question: str) -> str:
        return self._interaction_requester(
            "confirmation",
            question,
            ("确认并继续", "修正需求"),
        )

    # 先整理业务对齐复核问题，再在线程中等待用户确认或修正。
    async def _request_alignment_review_async(self, question: str) -> str:
        formatted_question = await self._format_user_message(question)
        return await asyncio.to_thread(
            self._request_alignment_review,
            formatted_question,
        )

    # 异步编排完整流水线；规划阶段选中的完整结果直接进入翻译，不再重复查询或塑形。
    async def run(self, user_question: str) -> AgentQueryPipelineResult:
        normalized_question = user_question.strip()
        if not normalized_question:
            raise ValueError("用户问题不能为空")

        schema_reader = self._schema_reader or CachingTableSchemaReader(
            InformationSchemaTableSchemaReader(
                self._settings,
                self._domain_profile.allowed_tables,
            ).read
        ).read
        self._emit(
            "alignment",
            "stage_started",
            "running",
            "正在理解查询需求",
            "正在将您的表达与当前业务规则进行对齐。",
        )
        alignment_subgraph = BusinessAlignmentSubgraph.from_settings(
            self._domain_profile,
            settings=self._settings,
            user_input_reader=self._request_clarification_async,
            alignment_review_reader=self._request_alignment_review_async,
            message_trace_queue=self._message_trace_queue,
            progress_emitter=self._progress_emitter,
        )
        alignment_result = await alignment_subgraph.run(
            normalized_question,
            max_generation_count=self._settings.agent_query_alignment_max_generations,
        )
        if alignment_result.status == "abandoned":
            assert alignment_result.abandonment is not None
            abandonment_message = await self._format_user_message(
                alignment_result.abandonment.user_message
            )
            return AgentQueryPipelineResult(
                status="abandoned",
                domain_key=self._domain_profile.key,
                user_question=normalized_question,
                alignment_result=alignment_result,
                user_message=abandonment_message,
            )

        assert alignment_result.aligned_request is not None
        self._emit(
            "alignment",
            "stage_completed",
            "success",
            "查询需求已整理",
            alignment_result.aligned_request.aligned_question,
        )
        self._emit(
            "planning",
            "stage_started",
            "running",
            "正在准备查询",
            "正在确定需要的数据、筛选范围和结果形式。",
        )
        planning_agent = DeepSeekQueryPlanningAgent.from_settings(
            self._domain_profile,
            settings=self._settings,
            schema_reader=schema_reader,
            user_input_reader=self._request_clarification_async,
            plan_review_reader=self._request_plan_review_async,
            trace_writer=self._trace_writer,
            message_trace_queue=self._message_trace_queue,
            progress_emitter=self._progress_emitter,
        )
        planning_result = await planning_agent.run(
            alignment_result.aligned_request.render_for_query_planning(),
            max_generation_count=self._settings.agent_query_planning_max_generations,
            max_tool_call_count=self._settings.agent_query_planning_max_tool_calls,
        )
        if planning_result.status == "abandoned":
            assert planning_result.abandonment is not None
            abandonment_message = await self._format_user_message(
                planning_result.abandonment.user_message
            )
            return AgentQueryPipelineResult(
                status="abandoned",
                domain_key=self._domain_profile.key,
                user_question=normalized_question,
                alignment_result=alignment_result,
                planning_result=planning_result,
                user_message=abandonment_message,
            )

        self._emit(
            "planning",
            "stage_completed",
            "success",
            "查询结果布局已确认",
            "原料查询和表格整理已经完成，正在转换业务状态。",
        )
        final_result = planning_result.final_result
        if final_result is None:
            return AgentQueryPipelineResult(
                status="failure",
                domain_key=self._domain_profile.key,
                user_question=normalized_question,
                alignment_result=alignment_result,
                planning_result=planning_result,
                error="查询规划未选择可进入翻译阶段的成功表格。",
            )
        sql_result = final_result.sql_result
        shaping_result = final_result.shaping_result
        self._emit(
            "translation",
            "stage_started",
            "running",
            "正在转换业务状态",
            "正在将最终表格中的状态和类型编码转换为可读业务含义。",
        )
        assert sql_result.analysis_sql is not None
        translation_subgraph = ResultTranslationSubgraph.from_settings(
            self._domain_profile,
            settings=self._settings,
            schema_reader=schema_reader,
            trace_writer=self._trace_writer,
            message_trace_queue=self._message_trace_queue,
        )
        result_column_sources = {
            column.key: column.source_result_field
            for column in shaping_result.columns
            if column.source_result_field is not None
        }
        translation_result = await translation_subgraph.run(
            sql=sql_result.analysis_sql,
            result_columns=[column.key for column in shaping_result.columns],
            rows=shaping_result.rows,
            schema_results=sql_result.schema_results,
            result_column_sources=result_column_sources,
        )
        translated_shaping_result = shaping_result.model_copy(
            update={"rows": translation_result.translated_rows}
        )
        if translation_result.status == "success":
            translation_message = (
                f"已完成 {len(translation_result.rules)} 个结果字段的业务含义转换。"
                if translation_result.rules
                else "当前结果字段无需额外转换。"
            )
            self._emit(
                "translation",
                "stage_completed",
                "success",
                "业务状态转换完成",
                translation_message,
            )
        else:
            self._emit(
                "translation",
                "stage_completed",
                "failure",
                "业务状态转换暂不可用",
                "最终表格将保留数据库原始值并继续生成结果。",
            )
        self._emit(
            "result",
            "stage_started",
            "running",
            "正在整理查询结果",
            "正在生成表格、统计信息和简洁说明。",
        )
        audit_subgraph = QueryResultAuditSubgraph.from_settings(
            self._domain_profile,
            settings=self._settings,
            trace_writer=self._trace_writer,
            message_trace_queue=self._message_trace_queue,
        )
        audit_result = await audit_subgraph.run(
            normalized_question,
            alignment_result,
            planning_result,
            sql_result,
            translated_shaping_result,
        )
        result_message = await self._format_user_message(
            _build_result_message(audit_result)
        )
        # 让 SSE、轨迹和结果接口继续共享同一摘要，只改变现有字符串的空白排版。
        if audit_result.status == "success" and audit_result.assessment is not None:
            audit_result = audit_result.model_copy(
                update={
                    "assessment": audit_result.assessment.model_copy(
                        update={"result_summary": result_message}
                    )
                }
            )
        self._emit(
            "result",
            "stage_completed",
            "success",
            "查询结果已完成",
            result_message,
        )
        return AgentQueryPipelineResult(
            status="success",
            domain_key=self._domain_profile.key,
            user_question=normalized_question,
            alignment_result=alignment_result,
            planning_result=planning_result,
            sql_result=sql_result,
            translation_result=translation_result,
            shaping_result=translated_shaping_result,
            audit_result=audit_result,
            user_message=result_message,
        )
