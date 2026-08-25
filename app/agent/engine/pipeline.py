"""编排业务对齐、查询规划、SQL 执行、结果翻译和结果审计的正式查询流水线。"""

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.domains.base import QueryDomainProfile
from app.agent.engine.business_alignment import (
    BusinessAlignmentResult,
    BusinessAlignmentSubgraph,
)
from app.agent.engine.query_planning import (
    DeepSeekQueryPlanningAgent,
    QueryPlanningAgentResult,
)
from app.agent.engine.result_audit import (
    QueryResultAuditResult,
    QueryResultAuditSubgraph,
)
from app.agent.engine.result_shaping import (
    ResultShapingSubgraph,
    ResultShapingSubgraphResult,
)
from app.agent.engine.result_translation import (
    ResultTranslationSubgraph,
    ResultTranslationSubgraphResult,
)
from app.agent.engine.sql_query import SqlQuerySubgraph, SqlQuerySubgraphResult
from app.agent.events.models import AgentProgressUpdate
from app.agent.events.publisher import ProgressEmitter
from app.agent.runtime.table_schema_cache import CachingTableSchemaReader
from app.agent.runtime.table_schema_reader import InformationSchemaTableSchemaReader
from app.agent.tools.table_schema import TableSchemaToolResponse
from app.core.config import Settings, get_settings


InteractionRequester = Callable[
    [Literal["confirmation", "clarification"], str, tuple[str, ...]],
    str,
]
TraceWriter = Callable[[str], None]
SchemaReader = Callable[[str], TableSchemaToolResponse]


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
    """以业务域配置装配共享工具和六个子图，并通过统一事件出口报告关键阶段。"""

    # 保存不可变业务域、运行预算和交互出口，实际模型与数据库适配器在单次运行中装配。
    def __init__(
        self,
        domain_profile: QueryDomainProfile,
        interaction_requester: InteractionRequester,
        progress_emitter: ProgressEmitter | None = None,
        settings: Settings | None = None,
        trace_writer: TraceWriter | None = None,
        schema_reader: SchemaReader | None = None,
    ) -> None:
        domain_profile.validate_resources()
        self._domain_profile = domain_profile
        self._interaction_requester = interaction_requester
        self._progress_emitter = progress_emitter
        self._settings = settings or get_settings()
        self._trace_writer = trace_writer
        self._schema_reader = schema_reader

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

    # 将子图的单问题回调转换为可暂停的正式交互请求，答案由查询会话恢复后同步返回。
    def _request_clarification(self, question: str) -> str:
        return self._interaction_requester("clarification", question, ())

    # 复核查询计划时只提供确认或修正选择，具体修正内容由后续独立澄清交互收集。
    def _request_plan_review(self, question: str) -> str:
        return self._interaction_requester(
            "confirmation",
            question,
            ("确认并继续", "修正查询"),
        )

    # 在对齐结果进入规划前强制请求用户确认，拒绝或修改不会消耗后续模型和数据库资源。
    def _confirm_alignment(self, aligned_question: str) -> bool:
        answer = self._interaction_requester(
            "confirmation",
            f"我将按以下需求继续查询：{aligned_question}",
            ("确认并继续", "取消查询"),
        )
        return answer.strip().lower() in {
            "确认并继续",
            "确认",
            "继续",
            "是",
            "yes",
            "y",
        }

    # 顺序运行对齐、规划、SQL、翻译、塑形和审计；翻译失败降级原值，塑形失败明确终止。
    def run(self, user_question: str) -> AgentQueryPipelineResult:
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
            user_input_reader=self._request_clarification,
            trace_writer=self._trace_writer,
            progress_emitter=self._progress_emitter,
        )
        alignment_result = alignment_subgraph.run(
            normalized_question,
            max_generation_count=self._settings.agent_query_alignment_max_generations,
        )
        if alignment_result.status == "abandoned":
            assert alignment_result.abandonment is not None
            return AgentQueryPipelineResult(
                status="abandoned",
                domain_key=self._domain_profile.key,
                user_question=normalized_question,
                alignment_result=alignment_result,
                user_message=alignment_result.abandonment.user_message,
            )

        assert alignment_result.aligned_request is not None
        self._emit(
            "alignment",
            "stage_completed",
            "success",
            "查询需求已整理",
            alignment_result.aligned_request.aligned_question,
        )
        if not self._confirm_alignment(
            alignment_result.aligned_request.aligned_question
        ):
            return AgentQueryPipelineResult(
                status="abandoned",
                domain_key=self._domain_profile.key,
                user_question=normalized_question,
                alignment_result=alignment_result,
                user_message="用户取消了本次查询。",
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
            user_input_reader=self._request_clarification,
            plan_review_reader=self._request_plan_review,
            trace_writer=self._trace_writer,
            progress_emitter=self._progress_emitter,
        )
        planning_result = planning_agent.run(
            alignment_result.aligned_request.render_for_query_planning(),
            max_generation_count=self._settings.agent_query_planning_max_generations,
            max_tool_call_count=self._settings.agent_query_planning_max_tool_calls,
        )
        if planning_result.status == "abandoned":
            assert planning_result.abandonment is not None
            return AgentQueryPipelineResult(
                status="abandoned",
                domain_key=self._domain_profile.key,
                user_question=normalized_question,
                alignment_result=alignment_result,
                planning_result=planning_result,
                user_message=planning_result.abandonment.user_message,
            )

        self._emit(
            "planning",
            "stage_completed",
            "success",
            "查询方案已准备",
            "需要的数据、筛选范围和结果形式已经确定。",
        )
        assert planning_result.query_plan is not None
        self._emit(
            "sql_generation",
            "stage_started",
            "running",
            "正在生成安全查询",
            "正在把查询方案转换为只读查询并执行安全检查。",
        )
        sql_subgraph = SqlQuerySubgraph.from_settings(
            self._domain_profile,
            settings=self._settings,
            schema_reader=schema_reader,
            trace_writer=self._trace_writer,
            progress_emitter=self._progress_emitter,
        )
        sql_result = sql_subgraph.run(
            planning_result.query_plan,
            planning_result.schema_results,
            max_generation_count=self._settings.agent_query_sql_max_generations,
        )
        if sql_result.status == "failure":
            return AgentQueryPipelineResult(
                status="failure",
                domain_key=self._domain_profile.key,
                user_question=normalized_question,
                alignment_result=alignment_result,
                planning_result=planning_result,
                sql_result=sql_result,
                error=sql_result.error or "查询生成或执行失败。",
            )

        self._emit(
            "execution",
            "stage_completed",
            "success",
            "数据读取完成",
            f"共读取到 {sql_result.returned_row_count} 条结果，正在整理展示内容。",
        )
        self._emit(
            "translation",
            "stage_started",
            "running",
            "正在转换业务状态",
            "正在将结果中的状态和类型编码转换为可读业务含义。",
        )
        assert sql_result.sql is not None
        assert sql_result.analysis_sql is not None
        translation_subgraph = ResultTranslationSubgraph.from_settings(
            self._domain_profile,
            settings=self._settings,
            schema_reader=schema_reader,
            trace_writer=self._trace_writer,
        )
        translation_result = translation_subgraph.run(
            sql=sql_result.analysis_sql,
            result_columns=sql_result.result_columns,
            rows=sql_result.rows,
            schema_results=sql_result.schema_results,
        )
        translated_sql_result = sql_result.model_copy(
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
                "查询结果将保留数据库原始值并继续生成表格。",
            )
        assert planning_result.result_shape_plan is not None
        self._emit(
            "shaping",
            "stage_started",
            "running",
            "正在整理表格结构",
            "正在按已确认的结果布局整理行列。",
        )
        shaping_result = ResultShapingSubgraph().run(
            planning_result.query_plan,
            planning_result.result_shape_plan,
            translated_sql_result.rows,
        )
        if shaping_result.status == "failure":
            self._emit(
                "shaping",
                "stage_completed",
                "failure",
                "表格结构整理失败",
                "查询数据已读取，但无法按确认的布局生成最终表格。",
            )
            return AgentQueryPipelineResult(
                status="failure",
                domain_key=self._domain_profile.key,
                user_question=normalized_question,
                alignment_result=alignment_result,
                planning_result=planning_result,
                sql_result=sql_result,
                translation_result=translation_result,
                shaping_result=shaping_result,
                error=shaping_result.error or "结果塑形失败。",
            )
        self._emit(
            "shaping",
            "stage_completed",
            "success",
            "表格结构整理完成",
            (
                f"已将 {shaping_result.source_row_count} 条查询数据整理为 "
                f"{shaping_result.result_row_count} 行结果。"
            ),
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
        )
        audit_result = audit_subgraph.run(
            normalized_question,
            alignment_result,
            planning_result,
            translated_sql_result,
            shaping_result,
        )
        result_message = _build_result_message(audit_result)
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
            shaping_result=shaping_result,
            audit_result=audit_result,
            user_message=result_message,
        )
