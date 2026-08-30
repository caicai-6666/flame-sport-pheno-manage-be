"""定义查询规划子图的状态、节点及其运行逻辑。"""

import asyncio
import json
import inspect
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent.text2sql.domains.base import QueryDomainProfile, QueryPlanPolicyIssue
from app.agent.text2sql.events.publisher import AgentProgressReporter, ProgressEmitter
from app.agent.text2sql.model_messages import (
    ModelMessageTraceQueue,
    create_traced_chat_completion,
)
from app.core.config import Settings, get_settings
from app.agent.text2sql.shared.model_options import (
    DEFAULT_PLANNING_MAX_TOKENS,
    get_model_request_profile,
    resolve_model_provider_connection,
)
from app.agent.text2sql.shared.system_guidance import (
    build_system_guidance_message,
)
from app.agent.text2sql.shared.tool_tag_template import (
    build_tool_tag_prefixed_task_content,
    load_tool_tag_template,
    resolve_query_tool_tag_template_filename,
)
from app.agent.text2sql.interaction.models import UserInteraction
from app.agent.text2sql.subgraphs.planning.tools.ask_user import (
    ASK_USER_TOOL_NAME,
    build_ask_user_tool_definition,
    parse_ask_user_tool_arguments,
)
from app.agent.text2sql.subgraphs.planning.inspection import SingleTableDataInspector
from app.agent.text2sql.subgraphs.planning.tools.table_inspection import (
    CLEAR_TABLE_DATA_INSPECTION_CONTEXT_TOOL_NAME,
    NEXT_TABLE_DATA_INSPECTION_PAGE_TOOL_NAME,
    TABLE_DATA_INSPECTION_TOOL_NAME,
    DataInspectionPurpose,
    TableDataInspectionResponse,
    build_clear_table_data_inspection_context_tool_definition,
    build_table_data_inspection_tool_definition,
    build_next_table_data_inspection_page_tool_definition,
    parse_clear_table_data_inspection_context_tool_arguments,
    parse_next_table_data_inspection_page_tool_arguments,
    parse_table_data_inspection_tool_arguments,
)
from app.agent.text2sql.subgraphs.planning.tools.query_plan import (
    ABANDON_QUERY_PLANNING_TOOL_NAME,
    NaturalLanguageQueryPlan,
    QueryPlanningAbandonment,
    ResultShapePlan,
    build_abandon_query_planning_tool_definition,
    parse_abandon_query_planning_arguments,
)
from app.agent.text2sql.subgraphs.planning.tools.material_plan import (
    MaterialQueryPlan,
    render_material_query_plan,
)
from app.agent.text2sql.subgraphs.planning.tools.material_data import (
    QUERY_MATERIAL_DATA_TOOL_NAME,
    SHAPE_MATERIAL_DATA_TOOL_NAME,
    SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME,
    QueryMaterialDataArguments,
    ShapeMaterialDataArguments,
    build_query_material_data_tool_definition,
    build_shape_material_data_tool_definition,
    build_submit_final_query_result_tool_definition,
    parse_query_material_data_tool_arguments,
    parse_shape_material_data_tool_arguments,
    parse_submit_final_query_result_tool_arguments,
)
from app.agent.text2sql.subgraphs.planning.markdown_preview import (
    MARKDOWN_PREVIEW_ROW_LIMIT,
    render_markdown_table_preview,
)
from app.agent.text2sql.subgraphs.planning.prompt import (
    build_query_planning_prompt,
)
from app.agent.text2sql.subgraphs.planning.tools.table_schema_reader import InformationSchemaTableSchemaReader
from app.agent.text2sql.subgraphs.planning.tools.table_schema_cache import CachingTableSchemaReader
from app.agent.text2sql.shared.yaml_context import parse_yaml_context, render_yaml_context
from app.agent.text2sql.subgraphs.planning.tools.table_schema import (
    TABLE_SCHEMA_TOOL_NAME,
    TableSchemaToolResponse,
    build_table_schema_tool_definition,
    ensure_allowed_table_name,
    parse_table_schema_tool_arguments,
)
from app.agent.text2sql.subgraphs.planning.tools.thinking import (
    THINKING_TOOL_NAME,
    ThinkingToolArguments,
    build_thinking_tool_definition,
    parse_thinking_tool_arguments,
)
from app.agent.text2sql.function_calling.feedback import (
    build_tool_argument_error_message,
)
from app.agent.text2sql.subgraphs.shaping.node import (
    MaterialResultShapingSubgraph,
    ResultShapingSubgraphResult,
)
from app.agent.text2sql.subgraphs.sql.models import MaterialSqlQueryPlan
from app.agent.text2sql.subgraphs.sql.node import (
    SqlQuerySubgraph,
    SqlQuerySubgraphResult,
)

DEFAULT_MAX_GENERATION_COUNT = 6
DEFAULT_MAX_TOOL_CALL_COUNT = 12
PLANNING_TOOL_CHOICE: Literal["auto"] = "auto"
PLANNING_REPEATED_THINK_GUIDANCE: Final[str] = (
    "你已经连续进行了两轮查询规划思考。请先判断是否仍有新的、会改变下一步动作的"
    "问题需要分析：如果有，请聚焦该问题继续思考；如果没有，请根据现有判断读取事实、"
    "询问用户、查询原料、塑形、提交最终结果或说明无法继续的原因，避免重复已有结论。"
)
PLAN_REVIEW_APPROVAL_ANSWERS = frozenset(
    {"确认并继续", "确认", "继续", "是", "没问题", "yes", "y"}
)
PLAN_REVIEW_CANCELLATION_ANSWERS = frozenset(
    {"取消查询", "取消", "停止查询", "不用了", "no", "n"}
)
PLAN_REVIEW_REVISION_ANSWERS = frozenset(
    {"修正查询", "修正", "调整查询", "修改查询"}
)
PLAN_REVISION_QUESTION = (
    "请说明需要怎样修正查询。您可以说明需要增加、删除或改名的字段，"
    "也可以调整结果布局或返回范围。"
)
SchemaReader = Callable[[str], TableSchemaToolResponse]
UserInputReader = Callable[[str], str | Awaitable[str]]
TraceWriter = Callable[[str], None]
DataInspector = Callable[
    [str, str, str, int, int, DataInspectionPurpose],
    TableDataInspectionResponse | Awaitable[TableDataInspectionResponse],
]
InspectionPageReader = Callable[
    [str], TableDataInspectionResponse | Awaitable[TableDataInspectionResponse]
]
MaterialQueryRunner = Callable[
    [QueryMaterialDataArguments, list[TableSchemaToolResponse]],
    SqlQuerySubgraphResult | Awaitable[SqlQuerySubgraphResult],
]
MaterialShapingRunner = Callable[
    [ShapeMaterialDataArguments, SqlQuerySubgraphResult],
    ResultShapingSubgraphResult | Awaitable[ResultShapingSubgraphResult],
]


# 对比业务对齐的量词与布局要求和双计划输出，阻止语义缺失的计划进入 SQL 层。
def _validate_query_plan_contract(
    planning_input: str,
    query_plan: NaturalLanguageQueryPlan,
    result_shape_plan: ResultShapePlan,
) -> tuple[QueryPlanPolicyIssue, ...]:
    try:
        planning_payload = parse_yaml_context(planning_input)
    except ValueError:
        return ()
    if not isinstance(planning_payload, dict):
        return ()
    aligned_query = planning_payload.get("aligned_query")
    if not isinstance(aligned_query, dict):
        return ()

    issues: list[QueryPlanPolicyIssue] = []
    aligned_constraints = aligned_query.get("logical_constraints")
    if isinstance(aligned_constraints, list):
        required_quantifiers = Counter(
            (item.get("quantifier"), item.get("count"))
            for item in aligned_constraints
            if isinstance(item, dict) and isinstance(item.get("quantifier"), str)
        )
        planned_quantifiers = Counter(
            (condition.quantifier, condition.count)
            for _, _, condition in query_plan.iter_quantified_conditions()
        )
        missing_quantifiers = required_quantifiers - planned_quantifiers
        if missing_quantifiers:
            missing_descriptions = [
                (
                    f"{quantifier}(count={count})"
                    if count is not None
                    else str(quantifier)
                )
                for (quantifier, count), occurrence_count in missing_quantifiers.items()
                for _ in range(occurrence_count)
            ]
            issues.append(
                QueryPlanPolicyIssue(
                    field_path="query_plan.query_blocks[].quantified_conditions",
                    message=(
                        "查询计划遗漏业务对齐层确认的集合量化条件："
                        + "、".join(missing_descriptions)
                    ),
                    repair_action=(
                        "在主体粒度正确的 query_block 中为上述每个量词新增一条 "
                        "quantified_conditions 项，"
                        "使用已读取的真实字段表达 predicate，并选择明确的实现方式。"
                    ),
                )
            )

    applied_business_rules = aligned_query.get("applied_business_rules")
    if isinstance(applied_business_rules, list):
        required_rule_ids = {
            rule_id for rule_id in applied_business_rules if isinstance(rule_id, str)
        }
        implemented_rule_ids = {
            implementation.rule_id
            for implementation in query_plan.implemented_business_rules
        }
        missing_rule_ids = sorted(required_rule_ids - implemented_rule_ids)
        if missing_rule_ids:
            issues.append(
                QueryPlanPolicyIssue(
                    field_path="query_plan.implemented_business_rules",
                    message=(
                        "查询计划没有落实业务对齐层采用的核心规则："
                        + "、".join(missing_rule_ids)
                    ),
                    repair_action=(
                        "为每个缺失规则新增 implemented_business_rules 项，"
                        "用带 query_block 作用域的 plan_references 指向实际落实该规则的 "
                        "filters、joins、quantified_conditions、having 或其他已有计划组件。"
                    ),
                )
            )

    if (
        aligned_query.get("result_scope") == "complete"
        and query_plan.pagination.limit is not None
    ):
        issues.append(
            QueryPlanPolicyIssue(
                field_path="query_plan.pagination.limit",
                message="用户要求完整导出，但 SQL 数据获取计划设置了行数上限。",
                repair_action=(
                    "将 query_plan.pagination.limit 改为 null，保持 offset 为 0，"
                    "完整返回符合筛选条件的数据。"
                ),
            )
        )

    for block, index, condition in query_plan.iter_quantified_conditions():
        condition_path = (
            f"query_plan.query_blocks[{block.block_id}]."
            f"quantified_conditions[{index}]"
        )
        normalized_predicate = re.sub(
            r"\s+",
            "",
            condition.predicate.replace("`", "").lower(),
        )
        matching_filter_indexes = [
            filter_index
            for filter_index, query_filter in enumerate(block.filters)
            if re.sub(
                r"\s+",
                "",
                query_filter.condition.replace("`", "").lower(),
            )
            == normalized_predicate
        ]
        if condition.quantifier in {"all", "none"} and matching_filter_indexes:
            issues.append(
                QueryPlanPolicyIssue(
                    field_path=(
                        f"query_plan.query_blocks[{block.block_id}].filters["
                        + ",".join(str(item) for item in matching_filter_indexes)
                        + "]"
                    ),
                    message=(
                        f"量词 {condition.quantifier} 的成员谓词被同时写成普通筛选，"
                        "这会在量化判断前删除反例，使全部或没有条件失真。"
                    ),
                    repair_action=(
                        f"从查询块 {block.block_id} 的 filters 删除与该量词 predicate "
                        "相同的普通筛选；filters 只保留集合范围条件，成员谓词仅在 "
                        "NOT EXISTS、条件聚合、子查询或 CTE 的资格判断中计算。"
                    ),
                )
            )
    presentation_requirements = aligned_query.get("presentation_requirements")
    required_layouts = {
        item.get("layout")
        for item in presentation_requirements or []
        if isinstance(item, dict)
    }
    if "pivot" in required_layouts and result_shape_plan.shape_type != "pivot":
        issues.append(
            QueryPlanPolicyIssue(
                field_path="result_shape_plan.shape_type",
                message="用户已明确要求动态按列展开，但塑形计划不是 pivot。",
                repair_action=(
                    "将 result_shape_plan.shape_type 改为 pivot，并完整填写分组字段、"
                    "透传字段、动态列取值、组内排序和列标题模板。"
                ),
            )
        )
    if required_layouts == {"table"} and result_shape_plan.shape_type != "passthrough":
        issues.append(
            QueryPlanPolicyIssue(
                field_path="result_shape_plan.shape_type",
                message="用户确认的是逐行普通表格，塑形计划却擅自改成了动态转列。",
                repair_action=(
                    "将 result_shape_plan.shape_type 改为 passthrough；"
                    "清空 group_fields 和 hidden_fields，把 pivot_value_field、"
                    "pivot_order_field、column_key_prefix、column_label_pattern、"
                    "expected_pivot_columns 全部改为 null。"
                ),
            )
        )
    selected_result_fields = [
        select_field.result_field for select_field in query_plan.select_fields
    ]
    consumed_result_fields = set(result_shape_plan.passthrough_fields)
    consumed_result_fields.update(result_shape_plan.group_fields)
    consumed_result_fields.update(result_shape_plan.hidden_fields)
    if result_shape_plan.pivot_value_field is not None:
        consumed_result_fields.add(result_shape_plan.pivot_value_field)
    if result_shape_plan.pivot_order_field is not None:
        consumed_result_fields.add(result_shape_plan.pivot_order_field)
    omitted_result_fields = [
        field_name
        for field_name in selected_result_fields
        if field_name not in consumed_result_fields
    ]
    if omitted_result_fields:
        issues.append(
            QueryPlanPolicyIssue(
                field_path="result_shape_plan.passthrough_fields",
                message=(
                    "塑形计划遗漏了根查询块已经返回的结果字段："
                    + "、".join(omitted_result_fields)
                ),
                repair_action=(
                    "按根查询块 select_fields 的原顺序，把以下字段加入 "
                    "result_shape_plan.passthrough_fields："
                    + "、".join(omitted_result_fields)
                    + "。不得让塑形层静默丢弃 SQL 已返回的用户结果字段。"
                ),
            )
        )
    if result_shape_plan.shape_type == "pivot":
        selected_fields_by_result = {
            select_field.result_field: select_field.field
            for select_field in query_plan.select_fields
        }
        stable_identity_fields = [
            field_name
            for field_name in result_shape_plan.group_fields
            if re.search(
                r"(?:^|\.)id\b",
                selected_fields_by_result.get(field_name, ""),
                flags=re.IGNORECASE,
            )
        ]
        if not stable_identity_fields:
            issues.append(
                QueryPlanPolicyIssue(
                    field_path="result_shape_plan.group_fields",
                    message=(
                        "pivot 塑形没有使用最终行主体的稳定 ID 分组；"
                        "仅按名称分组可能把同名主体合并为一行。"
                    ),
                    repair_action=(
                        f"在根查询块 {query_plan.root_block_id} 的 select_fields 中加入"
                        "最终行主体真实表的 id 字段，"
                        "为其声明稳定 result_field；把该 result_field 加入 "
                        "result_shape_plan.group_fields 和 hidden_fields。"
                        "名称等展示字段继续放在 passthrough_fields。"
                    ),
                )
            )
        requested_outputs = aligned_query.get("requested_outputs")
        identifier_requested = any(
            re.search(
                r"(?:(?<![A-Za-z0-9_])id(?![A-Za-z0-9_])|标识|编号|主键)",
                output,
                flags=re.IGNORECASE,
            )
            for output in requested_outputs or []
            if isinstance(output, str)
        )
        exposed_identity_fields = sorted(
            set(stable_identity_fields) & set(result_shape_plan.passthrough_fields)
        )
        if exposed_identity_fields and not identifier_requested:
            issues.append(
                QueryPlanPolicyIssue(
                    field_path="result_shape_plan.passthrough_fields",
                    message=(
                        "pivot 将仅用于稳定分组的主体 ID 暴露成了用户未要求的展示列："
                        + "、".join(exposed_identity_fields)
                    ),
                    repair_action=(
                        "从 passthrough_fields 删除上述 ID，并将其加入 hidden_fields；"
                        f"这些字段继续保留在 group_fields 和根查询块 "
                        f"{query_plan.root_block_id} 的 select_fields 中。"
                    ),
                )
            )
    return tuple(issues)


class PlanningFinalQueryResult(BaseModel):
    """保存 Planning 最终选中的完整查询与塑形结果，供翻译层继续处理。"""

    model_config = ConfigDict(extra="forbid")

    material_result_id: str = Field(description="被选中原料查询结果的唯一 ID")
    shaped_result_id: str = Field(description="被选中塑形结果的唯一 ID")
    selection_reason: str = Field(description="模型确认该塑形结果满足用户需求的理由")
    material_plan: MaterialQueryPlan = Field(
        description="由最终查询参数和塑形指导重建的兼容审计计划"
    )
    sql_result: SqlQuerySubgraphResult = Field(
        description="最终选中的完整 SQL 查询结果"
    )
    shaping_result: ResultShapingSubgraphResult = Field(
        description="最终选中的完整塑形结果"
    )

    # 最终载荷只能引用成功的 SQL 与塑形结果，避免失败或半成品进入翻译层。
    @model_validator(mode="after")
    def validate_successful_results(self) -> "PlanningFinalQueryResult":
        if self.sql_result.status != "success":
            raise ValueError("最终查询结果必须来自成功的原料查询")
        if self.shaping_result.status != "success":
            raise ValueError("最终塑形结果必须来自成功的塑形调用")
        return self


class QueryPlanningAgentResult(BaseModel):
    """一次查询规划产生的关键判断、事实读取结果和原料查询计划。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "abandoned"] = Field(
        default="success", description="规划成功或已按业务原因主动放弃"
    )
    abandonment: QueryPlanningAbandonment | None = Field(
        default=None, description="已放弃时面向用户的原因和提示"
    )
    thoughts: list[ThinkingToolArguments] = Field(description="模型记录的关键判断")
    schema_results: list[TableSchemaToolResponse] = Field(description="按需读取的表结构结果")
    data_inspections: list[TableDataInspectionResponse] = Field(
        description="按需读取的单表实际数据结果"
    )
    user_interactions: list[UserInteraction] = Field(
        description="规划阶段通过交互出口完成的事实澄清和结果字段复核"
    )
    material_plan: MaterialQueryPlan | None = Field(
        default=None,
        description="兼容审计使用的最终查询与塑形指导投影",
    )
    final_result: PlanningFinalQueryResult | None = Field(
        default=None,
        description="新运行时最终选中的完整查询与塑形结果",
    )
    query_plan: NaturalLanguageQueryPlan | None = Field(
        default=None, description="兼容历史运行结果的结构化查询计划；新规划不会再生成"
    )
    result_shape_plan: ResultShapePlan | None = Field(
        default=None, description="兼容历史运行结果的结构化塑形计划；新规划不会再生成"
    )
    query_request: str | None = Field(
        default=None, description="成功时可交给后续 SQL 执行器的联合查询自然语言"
    )
    raw_responses: list[str] = Field(description="每轮工具调用的原始模型响应")
    generation_count: int = Field(description="本次规划实际发起的模型生成次数")
    max_generation_count: int = Field(description="本次规划允许的最大模型生成次数")
    tool_call_count: int = Field(description="本次规划实际处理的工具调用次数")
    max_tool_call_count: int = Field(description="本次规划允许的最大工具调用次数")

    # 区分新原料计划与历史双计划，保证每个成功结果只有一种规划协议。
    @model_validator(mode="after")
    def validate_terminal_payload(self) -> "QueryPlanningAgentResult":
        if self.status == "success":
            if (
                self.material_plan is None
                and self.result_shape_plan is None
                and self.query_plan is not None
            ):
                self.result_shape_plan = ResultShapePlan(
                    passthrough_fields=[
                        field.result_field for field in self.query_plan.select_fields
                    ]
                )
            has_interactive_result = self.final_result is not None
            has_material_plan = (
                self.material_plan is not None and not has_interactive_result
            )
            has_legacy_plan = (
                self.query_plan is not None and self.result_shape_plan is not None
            )
            if sum(
                (has_interactive_result, has_material_plan, has_legacy_plan)
            ) != 1:
                raise ValueError("查询规划成功结果必须且只能包含一种查询计划")
            if has_interactive_result:
                assert self.final_result is not None
                if self.material_plan is None:
                    self.material_plan = self.final_result.material_plan
                elif self.material_plan != self.final_result.material_plan:
                    raise ValueError("顶层原料计划必须与最终选中结果一致")
            if has_material_plan and (
                self.query_plan is not None or self.result_shape_plan is not None
            ):
                raise ValueError("原料查询计划不能同时包含历史双计划")
            if has_interactive_result and (
                self.query_plan is not None or self.result_shape_plan is not None
            ):
                raise ValueError("交互式最终结果不能同时包含历史双计划")
            if self.query_request is None or self.abandonment is not None:
                raise ValueError("查询规划成功结果必须且只能包含查询计划")
        elif (
            self.abandonment is None
            or self.final_result is not None
            or self.material_plan is not None
            or self.query_plan is not None
            or self.result_shape_plan is not None
            or self.query_request is not None
        ):
            raise ValueError("查询规划放弃结果必须且只能包含 abandonment")
        return self


class QueryPlanningExecutionError(RuntimeError):
    """查询规划技术失败时保留已获取的原始模型响应，供联调产物回放。"""

    # 保存安全错误摘要和原始响应，避免终止工具参数异常掩盖已发生的模型与用户交互。
    def __init__(self, message: str, raw_responses: list[str]) -> None:
        super().__init__(message)
        self.raw_responses = raw_responses


class _QueryAgentState(TypedDict):
    """LangGraph 在工具循环前后传递的查询规划状态。"""

    user_question: str
    max_generation_count: int
    max_tool_call_count: int
    result: QueryPlanningAgentResult


# 将 OpenAI 兼容响应序列化为内部诊断输出，优先保留 SDK 提供的完整 JSON 结构。
def _serialize_raw_response(response: Any) -> str:
    model_dump_json = getattr(response, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json(indent=2)
    return repr(response)


# 将工具参数格式化为易读 JSON，参数并非 JSON 时仍保留模型返回的原始字符串。
def _format_tool_arguments(arguments: str) -> str:
    try:
        return json.dumps(json.loads(arguments), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return arguments


# 按工具名解析所有非终止工具参数，使任一 Schema 错误都能走统一的工具失败回传路径。
def _parse_nonterminal_tool_arguments(tool_name: str, arguments_json: str) -> Any:
    parsers: dict[str, Callable[[str], Any]] = {
        THINKING_TOOL_NAME: parse_thinking_tool_arguments,
        TABLE_SCHEMA_TOOL_NAME: parse_table_schema_tool_arguments,
        TABLE_DATA_INSPECTION_TOOL_NAME: parse_table_data_inspection_tool_arguments,
        NEXT_TABLE_DATA_INSPECTION_PAGE_TOOL_NAME: (
            parse_next_table_data_inspection_page_tool_arguments
        ),
        CLEAR_TABLE_DATA_INSPECTION_CONTEXT_TOOL_NAME: (
            parse_clear_table_data_inspection_context_tool_arguments
        ),
        ASK_USER_TOOL_NAME: parse_ask_user_tool_arguments,
        QUERY_MATERIAL_DATA_TOOL_NAME: parse_query_material_data_tool_arguments,
        SHAPE_MATERIAL_DATA_TOOL_NAME: parse_shape_material_data_tool_arguments,
    }
    parser = parsers.get(tool_name)
    if parser is None:
        raise KeyError(tool_name)
    return parser(arguments_json)


# 将单次模型响应整理为诊断日志，展示思考、文本和工具调用而不直接输出 SDK 原始对象。
def _format_model_trace(
    generation_index: int,
    max_generation_count: int,
    message: Any,
    tool_calls: list[Any],
) -> str:
    sections = [
        "\n" + "=" * 16 + f" 第 {generation_index} / {max_generation_count} 次模型调用 " + "=" * 16
    ]
    reasoning_content = getattr(message, "reasoning_content", None)
    if reasoning_content:
        sections.append(f"【模型思考】\n{reasoning_content}")
    content = getattr(message, "content", None)
    if content:
        sections.append(f"【模型输出】\n{content}")
    if tool_calls:
        tool_call_sections = []
        for tool_index, tool_call in enumerate(tool_calls, start=1):
            tool_call_sections.append(
                f"{tool_index}. `{tool_call.function.name}`\n"
                f"   调用 ID：{tool_call.id}\n"
                f"   参数：\n{_format_tool_arguments(tool_call.function.arguments)}"
            )
        sections.append("【工具调用】\n" + "\n".join(tool_call_sections))
    else:
        sections.append("【工具调用】\n模型未调用工具。")
    return "\n\n".join(sections)


# 将已确认检索结果的历史工具输出替换为最小占位内容，保留工具调用协议但释放候选行上下文。
def _clear_inspection_page_messages(
    messages: list[Any],
    page_tool_call_ids: dict[str, str],
    preserved_page_ids: set[str],
    cleared_tool_call_ids: set[str],
) -> int:
    cleared_count = 0
    tool_call_ids_to_clear = {
        tool_call_id
        for page_id, tool_call_id in page_tool_call_ids.items()
        if page_id not in preserved_page_ids
    }
    for message in messages:
        if not isinstance(message, dict):
            continue
        if (
            message.get("role") == "tool"
            and message.get("tool_call_id") in tool_call_ids_to_clear
            and message.get("tool_call_id") not in cleared_tool_call_ids
        ):
            message["content"] = render_yaml_context(
                {
                    "status": "success",
                    "result": "该单表检索页面内容已清除；请使用后续保留事实继续规划。",
                }
            )
            cleared_tool_call_ids.add(message["tool_call_id"])
            cleared_count += 1
    return cleared_count


# 在调用方未注入交互出口时明确终止，正式服务禁止退化为不可见的控制台输入。
async def _raise_missing_user_interaction(_: str) -> str:
    raise RuntimeError("查询规划需要用户回答，但未配置用户交互出口")


# 兼容同步测试替身和正式异步依赖，避免在事件循环中使用阻塞式等待包装。
async def _resolve_maybe_awaitable(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# 将供应商工具标签放在动态任务之前，降低普通文本退化并维持稳定请求前缀。
def _build_planning_task_message(
    user_question: str,
    tool_tag_template: str | None,
) -> dict[str, str]:
    return {
        "role": "user",
        "content": build_tool_tag_prefixed_task_content(
            user_question,
            tool_tag_template,
            (
                "本任务必须通过工具调用推进。请严格按照以下 tool-tag 结构输出，"
                "使服务端能够解析出 OpenAI tool_calls；不要把工具调用标签或参数"
                "写成普通正文。工具名和参数必须替换为本轮真实值。"
            ),
            "已对齐查询任务",
        ),
    }


# 在模型未产生 tool_call_id 时返回可重试协议错误，并按配置补充准确的标签格式。
def _build_missing_planning_tool_call_feedback(
    initial_think_completed: bool,
    tool_tag_template: str | None,
    required_tool_names: Counter[str] | None = None,
) -> dict[str, str]:
    if required_tool_names is not None:
        required_tools = "、".join(required_tool_names.elements())
        repair_action = (
            f"立即重新生成本轮响应，按上一条错误反馈修正参数，"
            f"并且只调用以下工具组合：{required_tools}。"
        )
    elif not initial_think_completed:
        repair_action = "立即重新生成本轮响应，并且只调用 think 提交一条关键判断。"
    else:
        repair_action = "立即重新生成本轮响应，并至少调用一个当前提供的工具推进查询规划。"
    error: dict[str, Any] = {
        "code": "planning_tool_call_missing",
        "message": "本轮没有调用工具，普通文本不能推进查询规划流程。",
        "repair_action": repair_action,
    }
    if tool_tag_template is not None:
        error["repair_action"] = (
            f"{repair_action} 必须严格按照下方 tool-tag 格式输出，"
            "确保服务端解析出 tool_calls。"
        )
        error["tool_call_format_guidance"] = {
            "instruction": "仿照 template 的标签结构输出真实工具名和合法 JSON 参数。",
            "template": tool_tag_template,
        }
    return {
        "role": "user",
        "content": json.dumps(
            {
                "context_type": "workflow_protocol_feedback",
                "status": "failure",
                "error": error,
                "retryable": True,
            },
            ensure_ascii=False,
        ),
    }


# 将调用顺序、组合或工具名错误作为对应 tool_call_id 的普通失败结果回传模型。
def _build_planning_protocol_tool_error(
    tool_call_id: str,
    tool_name: str,
    error_code: str,
    message: str,
    repair_action: str,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": error_code,
                    "tool_name": tool_name,
                    "message": message,
                    "details": [
                        {
                            "error_type": "tool_protocol_violation",
                            "field_path": "$",
                            "message": message,
                            "repair_action": repair_action,
                        }
                    ],
                },
                "retryable": True,
                "next_action": repair_action,
            },
            ensure_ascii=False,
        ),
    }


# 修正成功后移除此前无效调用及错误反馈，避免已解决错误继续污染后续模型上下文。
def _remove_repaired_planning_context(
    messages: list[Any],
    repair_context_start: int | None,
    corrected_turn_start: int,
) -> bool:
    if repair_context_start is None:
        return False
    del messages[repair_context_start:corrected_turn_start]
    return True


# 仅判断当前成功调用是否解决了原失败工具，用于清理上下文而不限制模型的工具选择。
def _resolves_planning_repair_origin(
    tool_calls: list[Any],
    repair_origin_tool_names: Counter[str] | None,
) -> bool:
    if repair_origin_tool_names is None:
        return True
    return Counter(
        tool_call.function.name for tool_call in tool_calls
    ) == repair_origin_tool_names


# 按塑形计划解析最终可见表头，隐藏技术字段并把动态转列标题转换为用户可理解的范围。
def _build_visible_result_labels(
    query_plan: NaturalLanguageQueryPlan,
    result_shape_plan: ResultShapePlan,
) -> list[str]:
    labels_by_result_field = {
        field.result_field: field.purpose for field in query_plan.select_fields
    }
    visible_labels = [
        labels_by_result_field.get(field_name, field_name)
        for field_name in result_shape_plan.passthrough_fields
    ]
    if result_shape_plan.shape_type == "pivot":
        assert result_shape_plan.column_label_pattern is not None
        expected_columns = result_shape_plan.expected_pivot_columns
        if expected_columns is not None and expected_columns <= 6:
            visible_labels.extend(
                result_shape_plan.column_label_pattern.replace("{index}", str(index))
                for index in range(1, expected_columns + 1)
            )
        elif expected_columns is not None:
            visible_labels.append(
                result_shape_plan.column_label_pattern.replace(
                    "{index}", f"1～{expected_columns}"
                )
            )
        else:
            visible_labels.append(
                result_shape_plan.column_label_pattern.replace("{index}", "1、2、3……")
            )
    return visible_labels


# 在 SSE 消息长度边界内尽量展示全部表头，字段过多时明确给出尚未展开的数量。
def _render_bounded_result_labels(visible_labels: list[str]) -> str:
    field_text = "、".join(visible_labels) or "无可见字段"
    if len(field_text) <= 260:
        return field_text

    retained_labels: list[str] = []
    retained_length = 0
    for label in visible_labels:
        additional_length = len(label) + (1 if retained_labels else 0)
        if retained_length + additional_length > 220:
            break
        retained_labels.append(label)
        retained_length += additional_length
    omitted_count = len(visible_labels) - len(retained_labels)
    return "、".join(retained_labels) + f"……（另有 {omitted_count} 个字段）"


# 判断规划复核答案是否为受支持的肯定表达，避免把快捷确认再次交给模型消耗生成次数。
def _is_query_plan_review_approved(answer: str) -> bool:
    return answer.strip().lower() in PLAN_REVIEW_APPROVAL_ANSWERS


# 判断规划复核答案是否明确要求停止，其他自由文本统一作为计划修订意见处理。
def _is_query_plan_review_cancelled(answer: str) -> bool:
    return answer.strip().lower() in PLAN_REVIEW_CANCELLATION_ANSWERS


# 判断用户是否选择进入独立修正说明步骤，使固定选项和自由文本不会混在同一次交互中。
def _is_query_plan_revision_requested(answer: str) -> bool:
    return answer.strip().lower() in PLAN_REVIEW_REVISION_ANSWERS


# 把用户的修改意见作为最终选择工具的正常结果回传，使模型按反馈重新查询或塑形。
def _build_query_plan_revision_message(
    tool_call_id: str,
    review_question: str,
    user_feedback: str,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": render_yaml_context(
            {
                "status": "revision_requested",
                "result": {
                    "message": "用户尚未确认当前查询方案，需要根据反馈修订后重新提交。",
                    "previous_result_preview": review_question,
                    "user_feedback": user_feedback,
                    "next_action": (
                        "保留未被反馈否定的查询口径。若反馈涉及筛选或缺少原料，重新调用 "
                        "query_material_data；若仅涉及行列布局，使用合适的原料结果重新调用 "
                        "shape_material_data。观察新结果后，再调用 submit_final_query_result。"
                    ),
                },
            }
        ),
    }


@dataclass(frozen=True, slots=True)
class _MaterialResultEntry:
    """关联一次成功原料查询的参数和后台完整 SQL 结果。"""

    arguments: QueryMaterialDataArguments
    sql_result: SqlQuerySubgraphResult


@dataclass(frozen=True, slots=True)
class _ShapedResultEntry:
    """关联一次成功塑形的来源原料、指导和后台完整结果。"""

    material_result_id: str
    shaping_guidance: str
    shaping_result: ResultShapingSubgraphResult


# 为当前 Planning 会话创建不可猜测的结果引用，避免不同查询之间误用顺序编号。
def _create_planning_result_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


# 将成功原料查询的真实表头和前五行渲染为模型可直接观察的 Markdown 表格。
def _render_material_query_success(
    material_result_id: str,
    sql_result: SqlQuerySubgraphResult,
) -> str:
    preview = render_markdown_table_preview(
        sql_result.result_columns,
        sql_result.rows,
    )
    preview_count = min(
        sql_result.returned_row_count,
        MARKDOWN_PREVIEW_ROW_LIMIT,
    )
    return (
        "【查询状态】成功\n\n"
        f"- 原料结果 ID：`{material_result_id}`\n"
        f"- 完整结果行数：{sql_result.returned_row_count}\n"
        f"- 当前展示：前 {preview_count} 行\n"
        f"- 是否达到查询上限：{'是' if sql_result.limit_reached else '否'}\n\n"
        f"{preview}\n\n"
        "完整结果未写入模型上下文。后续塑形必须使用上述原料结果 ID。"
    )


# 把 SQL 子图的安全错误摘要转换为 Planning 可修正下一轮调用的友好工具反馈。
def _render_material_query_failure(sql_result: SqlQuerySubgraphResult) -> str:
    if sql_result.retry_target == "query_planning":
        repair_action = (
            "根据错误补全或修正查询指导与所需表，再使用更精确的参数重新查询。"
        )
    elif sql_result.retry_target == "sql_generation":
        repair_action = (
            "保留正确业务口径，进一步明确必取字段、筛选和关联要求后重新查询。"
        )
    else:
        repair_action = (
            "该问题未必能通过修改业务指导解决；不要原样重试，必要时结束本次查询。"
        )
    return (
        "【查询状态】失败\n\n"
        f"- 错误代码：{sql_result.error_code or 'material_query_failed'}\n"
        f"- 失败原因：{sql_result.error or '原料查询未产生可用结果。'}\n"
        f"- 修正提示：{repair_action}\n\n"
        "本次失败未生成原料结果 ID，不能进入塑形。"
    )


# 将原料工具调用前即可确定的业务域或运行配置错误转换为不会产生结果 ID 的反馈。
def _render_material_query_request_failure(
    reason: str,
    repair_action: str,
) -> str:
    return (
        "【查询状态】失败\n\n"
        f"- 失败原因：{reason}\n"
        f"- 修正提示：{repair_action}\n\n"
        "本次失败未执行数据库查询，也未生成原料结果 ID。"
    )


# 将成功塑形后的中文表头和前五行渲染为模型可直接复核的 Markdown 表格。
def _render_material_shaping_success(
    shaped_result_id: str,
    material_result_id: str,
    shaping_result: ResultShapingSubgraphResult,
) -> str:
    headers = [column.label for column in shaping_result.columns]
    column_keys = [column.key for column in shaping_result.columns]
    preview = render_markdown_table_preview(
        headers,
        shaping_result.rows,
        column_keys=column_keys,
    )
    preview_count = min(
        shaping_result.result_row_count,
        MARKDOWN_PREVIEW_ROW_LIMIT,
    )
    return (
        "【塑形状态】成功\n\n"
        f"- 塑形结果 ID：`{shaped_result_id}`\n"
        f"- 来源原料结果 ID：`{material_result_id}`\n"
        f"- 完整结果行数：{shaping_result.result_row_count}\n"
        f"- 当前展示：前 {preview_count} 行\n\n"
        f"{preview}\n\n"
        "完整塑形结果未写入模型上下文。确认满足需求后可选择上述塑形结果 ID。"
    )


# 把塑形子图失败转换为可重试提示，并明确失败调用不会产生塑形结果引用。
def _render_material_shaping_failure(
    material_result_id: str,
    shaping_result: ResultShapingSubgraphResult,
) -> str:
    return (
        "【塑形状态】失败\n\n"
        f"- 来源原料结果 ID：`{material_result_id}`\n"
        f"- 失败原因：{shaping_result.error or '塑形工具未产生可用结果。'}\n"
        "- 修正提示：若原料列完整，请更精确地说明最终行粒度、字段、分组、"
        "排序和动态列后重新塑形；若反馈指出缺少原料，请重新查询。\n\n"
        "本次失败未生成塑形结果 ID，不能提交为最终结果。"
    )


# 将无效原料引用或塑形运行配置错误转换为可定位且不会泄露完整数据的反馈。
def _render_material_shaping_request_failure(
    material_result_id: str,
    reason: str,
    repair_action: str,
) -> str:
    return (
        "【塑形状态】失败\n\n"
        f"- 来源原料结果 ID：`{material_result_id}`\n"
        f"- 失败原因：{reason}\n"
        f"- 修正提示：{repair_action}\n\n"
        "本次失败未生成塑形结果 ID，不能提交为最终结果。"
    )


# 将无效最终结果引用作为终止工具的普通失败结果返回，使模型仍可选择真实成功结果。
def _render_final_result_reference_failure(
    shaped_result_id: str,
    available_result_ids: list[str],
) -> str:
    available_text = (
        "、".join(f"`{result_id}`" for result_id in available_result_ids)
        if available_result_ids
        else "当前还没有成功的塑形结果"
    )
    return (
        "【最终选择状态】失败\n\n"
        f"- 失败原因：塑形结果 ID `{shaped_result_id}` 不存在或并非本轮成功结果。\n"
        f"- 当前可选结果：{available_text}\n"
        "- 修正提示：只能提交本轮 shape_material_data 成功返回且已经观察过的结果 ID；"
        "若没有可选结果，请先完成原料查询和塑形。"
    )


# 使用已经生成的真实塑形表头构造用户复核问题，不暴露 SQL 或内部模型轨迹。
def _build_final_result_review_question(
    shaping_result: ResultShapingSubgraphResult,
) -> str:
    labels = [column.label for column in shaping_result.columns]
    return (
        "最终查询表格已准备好。\n"
        f"结果行数：{shaping_result.result_row_count}\n"
        f"结果字段：{_render_bounded_result_labels(labels)}\n"
        "请选择‘确认并继续’或‘修正查询’。"
    )


class DeepSeekQueryPlanningAgent:
    """在异步工具循环中按需读取事实，直到生成最终联合查询自然语言。"""

    # 初始化模型、表概览、结构读取器及 LangGraph 工作流，便于测试替换外部依赖。
    def __init__(
        self,
        client: Any,
        model: str,
        domain_profile: QueryDomainProfile,
        schema_reader: SchemaReader,
        user_input_reader: UserInputReader = _raise_missing_user_interaction,
        plan_review_reader: UserInputReader | None = None,
        trace_writer: TraceWriter | None = None,
        message_trace_queue: ModelMessageTraceQueue | None = None,
        data_inspector: DataInspector | None = None,
        inspection_page_reader: InspectionPageReader | None = None,
        material_query_runner: MaterialQueryRunner | None = None,
        material_shaping_runner: MaterialShapingRunner | None = None,
        progress_emitter: ProgressEmitter | None = None,
        max_tokens: int = DEFAULT_PLANNING_MAX_TOKENS,
        request_profile: str = "deepseek",
        tool_tag_template: str | None = None,
        close_client_after_run: bool = False,
    ) -> None:
        self._client = client
        self._model = model
        self._domain_profile = domain_profile
        self._allowed_tables = frozenset(domain_profile.allowed_tables)
        self._schema_reader = schema_reader
        self._user_input_reader = user_input_reader
        self._plan_review_reader = plan_review_reader or user_input_reader
        self._trace_writer = trace_writer
        self._message_trace_queue = message_trace_queue
        self._data_inspector = data_inspector
        self._inspection_page_reader = inspection_page_reader
        self._material_query_runner = material_query_runner
        self._material_shaping_runner = material_shaping_runner
        self._progress_reporter = AgentProgressReporter(
            domain_profile,
            progress_emitter,
        )
        self._max_tokens = max_tokens
        self._request_profile = get_model_request_profile(request_profile)
        self._tool_tag_template = tool_tag_template
        self._close_client_after_run = close_client_after_run
        self._base_prompt = build_query_planning_prompt(domain_profile)

        workflow = StateGraph(_QueryAgentState)
        workflow.add_node("run_tool_loop", self._run_tool_loop)
        workflow.add_edge(START, "run_tool_loop")
        workflow.add_edge("run_tool_loop", END)
        self._workflow = workflow.compile()

    # 使用全局供应商配置创建标准异步客户端，并让规划工具与单表检索共享结构读取器和协议模板。
    @classmethod
    def from_settings(
        cls,
        domain_profile: QueryDomainProfile,
        settings: Settings | None = None,
        schema_reader: SchemaReader | None = None,
        user_input_reader: UserInputReader = _raise_missing_user_interaction,
        plan_review_reader: UserInputReader | None = None,
        trace_writer: TraceWriter | None = None,
        message_trace_queue: ModelMessageTraceQueue | None = None,
        progress_emitter: ProgressEmitter | None = None,
    ) -> "DeepSeekQueryPlanningAgent":
        resolved_settings = settings or get_settings()
        connection = resolve_model_provider_connection(resolved_settings)
        client = AsyncOpenAI(
            api_key=connection.api_key,
            base_url=connection.base_url,
            timeout=connection.timeout_seconds,
        )
        schema_reader = schema_reader or CachingTableSchemaReader(
            InformationSchemaTableSchemaReader(
                resolved_settings,
                domain_profile.allowed_tables,
            ).read
        ).read
        data_inspector = SingleTableDataInspector(
            client=client,
            model=connection.model,
            settings=resolved_settings,
            domain_profile=domain_profile,
            schema_reader=schema_reader,
            trace_writer=trace_writer,
            message_trace_queue=message_trace_queue,
            max_tokens=resolved_settings.deepseek_query_inspection_max_tokens,
            request_profile=connection.provider,
            tool_tag_template=load_tool_tag_template(
                resolve_query_tool_tag_template_filename(resolved_settings)
            ),
        )

        # 每次工具调用创建独立 SQL 子图，使其有限重试和客户端生命周期互不干扰。
        async def run_material_query(
            arguments: QueryMaterialDataArguments,
            schema_results: list[TableSchemaToolResponse],
        ) -> SqlQuerySubgraphResult:
            sql_subgraph = SqlQuerySubgraph.from_settings(
                domain_profile,
                settings=resolved_settings,
                schema_reader=schema_reader,
                trace_writer=trace_writer,
                message_trace_queue=message_trace_queue,
                progress_emitter=progress_emitter,
            )
            return await sql_subgraph.run(
                MaterialSqlQueryPlan(
                    guidance=arguments.guidance,
                    required_tables=arguments.required_tables,
                ),
                schema_results,
                max_generation_count=(
                    resolved_settings.agent_query_sql_max_generations
                ),
            )

        # 每次工具调用创建独立塑形子图，并始终使用指定原料 ID 对应的完整查询结果。
        async def run_material_shaping(
            arguments: ShapeMaterialDataArguments,
            sql_result: SqlQuerySubgraphResult,
        ) -> ResultShapingSubgraphResult:
            shaping_subgraph = MaterialResultShapingSubgraph.from_settings(
                domain_profile,
                settings=resolved_settings,
                trace_writer=trace_writer,
                message_trace_queue=message_trace_queue,
            )
            return await shaping_subgraph.run(
                arguments.shaping_guidance,
                sql_result.result_columns,
                sql_result.rows,
            )

        return cls(
            client=client,
            model=connection.model,
            domain_profile=domain_profile,
            schema_reader=schema_reader,
            user_input_reader=user_input_reader,
            plan_review_reader=plan_review_reader,
            trace_writer=trace_writer,
            message_trace_queue=message_trace_queue,
            data_inspector=data_inspector.inspect,
            inspection_page_reader=data_inspector.get_next_page,
            material_query_runner=run_material_query,
            material_shaping_runner=run_material_shaping,
            progress_emitter=progress_emitter,
            max_tokens=resolved_settings.deepseek_query_planning_max_tokens,
            request_profile=connection.provider,
            tool_tag_template=load_tool_tag_template(
                resolve_query_tool_tag_template_filename(resolved_settings)
            ),
            close_client_after_run=True,
        )

    # 在显式启用内部追踪时记录模型、工具和执行结果，默认不向标准输出泄漏技术轨迹。
    def _write_trace(self, content: str) -> None:
        if self._trace_writer is not None:
            self._trace_writer(content)

    # 异步运行规划工具循环，以 auto 模式接受调用并用本地状态机反馈可修复协议错误。
    async def _run_tool_loop(
        self,
        state: _QueryAgentState,
    ) -> dict[str, QueryPlanningAgentResult]:
        messages: list[Any] = [
            {"role": "system", "content": self._base_prompt},
            _build_planning_task_message(
                state["user_question"],
                self._tool_tag_template,
            ),
        ]
        tools = [
            build_thinking_tool_definition(),
            build_table_schema_tool_definition(self._domain_profile.allowed_tables),
            build_table_data_inspection_tool_definition(
                self._domain_profile.allowed_tables
            ),
            build_next_table_data_inspection_page_tool_definition(),
            build_clear_table_data_inspection_context_tool_definition(),
            build_ask_user_tool_definition(),
            build_query_material_data_tool_definition(),
            build_shape_material_data_tool_definition(),
            build_submit_final_query_result_tool_definition(),
            build_abandon_query_planning_tool_definition(
                self._domain_profile.query_scope
            ),
        ]
        thoughts: list[ThinkingToolArguments] = []
        schema_results: list[TableSchemaToolResponse] = []
        data_inspections: list[TableDataInspectionResponse] = []
        user_interactions: list[UserInteraction] = []
        raw_responses: list[str] = []
        material_results: dict[str, _MaterialResultEntry] = {}
        shaped_results: dict[str, _ShapedResultEntry] = {}
        inspection_page_tool_call_ids_by_id: dict[str, dict[str, str]] = {}
        inspection_has_more_by_id: dict[str, bool] = {}
        cleared_inspection_tool_call_ids: set[str] = set()
        max_generation_count = state["max_generation_count"]
        max_tool_call_count = state["max_tool_call_count"]

        tool_call_count = 0
        generation_count = 0
        initial_think_completed = False
        consecutive_think_count = 0
        pending_system_guidance: dict[str, str] | None = None
        repair_context_start: int | None = None
        repair_origin_tool_names: Counter[str] | None = None
        while generation_count < max_generation_count:
            generation_count += 1
            generation_index = generation_count
            current_turn_start = len(messages)
            response = await create_traced_chat_completion(
                client=self._client,
                message_queue=self._message_trace_queue,
                node="planning",
                model=self._model,
                messages=messages,
                tools=tools,
                tool_choice=PLANNING_TOOL_CHOICE,
                **self._request_profile.build_non_thinking_options(self._max_tokens),
            )
            # 系统指导只作用于紧邻的一次模型请求，响应返回后立即移除以免污染长期上下文。
            if pending_system_guidance is not None:
                if messages[-1] is not pending_system_guidance:
                    raise RuntimeError("系统指导消息上下文位置异常")
                messages.pop()
                current_turn_start -= 1
                pending_system_guidance = None
            raw_responses.append(_serialize_raw_response(response))
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            self._write_trace(
                _format_model_trace(
                    generation_index,
                    max_generation_count,
                    message,
                    tool_calls,
                )
            )
            if not tool_calls:
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                    repair_origin_tool_names = (
                        Counter({THINKING_TOOL_NAME: 1})
                        if not initial_think_completed
                        else None
                    )
                feedback_message = _build_missing_planning_tool_call_feedback(
                    initial_think_completed,
                    self._tool_tag_template,
                    None,
                )
                messages.extend((message, feedback_message))
                self._write_trace(
                    "\n----- 查询规划工具协议校验结果 -----\n"
                    "error_code: planning_tool_call_missing\n"
                    f"result: {feedback_message['content']}"
                )
                continue
            if tool_call_count + len(tool_calls) > max_tool_call_count:
                raise QueryPlanningExecutionError(
                    f"工具调用超过最大次数：{max_tool_call_count}",
                    raw_responses,
                )
            tool_call_count += len(tool_calls)

            messages.append(message)
            registered_tool_names = {
                THINKING_TOOL_NAME,
                TABLE_SCHEMA_TOOL_NAME,
                TABLE_DATA_INSPECTION_TOOL_NAME,
                NEXT_TABLE_DATA_INSPECTION_PAGE_TOOL_NAME,
                CLEAR_TABLE_DATA_INSPECTION_CONTEXT_TOOL_NAME,
                ASK_USER_TOOL_NAME,
                QUERY_MATERIAL_DATA_TOOL_NAME,
                SHAPE_MATERIAL_DATA_TOOL_NAME,
                SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME,
                ABANDON_QUERY_PLANNING_TOOL_NAME,
            }
            tool_name_counts = Counter(
                tool_call.function.name for tool_call in tool_calls
            )
            terminal_tool_calls = [
                tool_call
                for tool_call in tool_calls
                if tool_call.function.name
                in {
                    SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME,
                    ABANDON_QUERY_PLANNING_TOOL_NAME,
                }
            ]
            protocol_error: tuple[str, str, str] | None = None
            protocol_repair_origin: Counter[str] | None = None
            unknown_tool_names = sorted(set(tool_name_counts) - registered_tool_names)
            if unknown_tool_names:
                protocol_error = (
                    "planning_unknown_tool",
                    "本轮包含未注册工具：" + "、".join(unknown_tool_names) + "。",
                    "从本轮提供的工具列表中重新选择；不要输出或调用未注册工具。",
                )
            elif not initial_think_completed and tool_name_counts != Counter(
                {THINKING_TOOL_NAME: 1}
            ):
                protocol_error = (
                    "planning_initial_think_required",
                    "首个有效工具调用必须且只能是一次 think，本轮调用未执行。",
                    "只调用 think，提交一条影响下一步工具选择的简短关键判断。",
                )
                protocol_repair_origin = Counter({THINKING_TOOL_NAME: 1})
            elif terminal_tool_calls and len(tool_calls) != 1:
                protocol_error = (
                    "planning_terminal_tool_must_be_single",
                    "终止工具必须单独调用，不能与任何其他工具处于同一响应。",
                    "重新生成本轮响应，并且只调用一个所需的终止工具。",
                )
                if len(terminal_tool_calls) == 1:
                    protocol_repair_origin = Counter(
                        {terminal_tool_calls[0].function.name: 1}
                    )
            elif THINKING_TOOL_NAME in tool_name_counts and len(tool_calls) != 1:
                protocol_error = (
                    "planning_think_tool_must_be_single",
                    "think 必须单独调用，不能与事实查询或终止工具并行。",
                    "本轮只调用 think；收到结果后再调用下一项工具。",
                )
                protocol_repair_origin = Counter({THINKING_TOOL_NAME: 1})
            elif ASK_USER_TOOL_NAME in tool_name_counts and len(tool_calls) != 1:
                protocol_error = (
                    "planning_ask_user_must_be_single",
                    "ask_user 必须单独调用，不能在同一响应中执行其他工具。",
                    "本轮只调用 ask_user 提出一个明确问题。",
                )
                protocol_repair_origin = Counter({ASK_USER_TOOL_NAME: 1})
            elif (
                QUERY_MATERIAL_DATA_TOOL_NAME in tool_name_counts
                or SHAPE_MATERIAL_DATA_TOOL_NAME in tool_name_counts
            ) and len(tool_calls) != 1:
                protocol_error = (
                    "planning_data_tool_must_be_single",
                    "原料查询和塑形工具必须单独调用，不能与任何其他工具并行。",
                    "重新生成本轮响应，并且只调用一个所需的数据处理工具。",
                )

            if protocol_error is not None:
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                    repair_origin_tool_names = protocol_repair_origin
                protocol_failures: list[tuple[Any, dict[str, str]]] = []
                for tool_call in tool_calls:
                    error_message = _build_planning_protocol_tool_error(
                        tool_call.id,
                        tool_call.function.name,
                        *protocol_error,
                    )
                    protocol_failures.append((tool_call, error_message))
                    self._write_trace(
                        "\n----- 查询规划工具协议校验结果 -----\n"
                        f"tool_call_id: {tool_call.id}\n"
                        f"tool_name: {tool_call.function.name}\n"
                        f"result: {error_message['content']}"
                    )
                messages.extend(
                    error_message for _, error_message in protocol_failures
                )
                continue

            # 任一协议合法的非 think 动作都会结束本段连续思考，后续重新从零计数。
            if THINKING_TOOL_NAME not in tool_name_counts:
                consecutive_think_count = 0

            final_result_call = next(
                (
                    tool_call
                    for tool_call in tool_calls
                    if tool_call.function.name == SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME
                ),
                None,
            )
            parsed_nonterminal_arguments: dict[str, Any] = {}
            nonterminal_argument_errors: dict[str, ValidationError] = {}
            for tool_call in tool_calls:
                if tool_call.function.name in {
                    SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME,
                    ABANDON_QUERY_PLANNING_TOOL_NAME,
                }:
                    continue
                try:
                    parsed_nonterminal_arguments[tool_call.id] = (
                        _parse_nonterminal_tool_arguments(
                            tool_call.function.name,
                            tool_call.function.arguments,
                        )
                    )
                except ValidationError as error:
                    nonterminal_argument_errors[tool_call.id] = error
            if nonterminal_argument_errors:
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                    repair_origin_tool_names = tool_name_counts
                argument_failures: list[tuple[Any, dict[str, str]]] = []
                for tool_call in tool_calls:
                    validation_error = nonterminal_argument_errors.get(tool_call.id)
                    if validation_error is not None:
                        error_message = build_tool_argument_error_message(
                            tool_call.id,
                            tool_call.function.name,
                            validation_error,
                        )
                    else:
                        error_message = _build_planning_protocol_tool_error(
                            tool_call.id,
                            tool_call.function.name,
                            "planning_atomic_tool_batch_rejected",
                            "同一响应中的另一项工具参数不合法，因此本轮全部工具均未执行。",
                            "保留本轮原意，修正错误参数后重新提交完整工具组合。",
                        )
                    argument_failures.append((tool_call, error_message))
                    self._write_trace(
                        f"\n----- 第 {generation_index} 次模型调用的工具参数校验结果 -----\n"
                        f"tool_call_id: {tool_call.id}\n"
                        f"tool_name: {tool_call.function.name}\n"
                        f"result: {error_message['content']}"
                    )
                messages.extend(
                    error_message for _, error_message in argument_failures
                )
                continue
            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                if tool_name in {
                    SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME,
                    ABANDON_QUERY_PLANNING_TOOL_NAME,
                }:
                    continue
                arguments = parsed_nonterminal_arguments[tool_call.id]

                if tool_name == THINKING_TOOL_NAME:
                    thought = arguments
                    thoughts.append(thought)
                    initial_think_completed = True
                    consecutive_think_count += 1
                    self._progress_reporter.reasoning_progress("planning")
                    tool_result = {
                        "status": "success",
                        "result": f"已记录本轮关键判断：{thought.reason}",
                    }
                elif tool_name == TABLE_SCHEMA_TOOL_NAME:
                    self._progress_reporter.schema_lookup_started(
                        arguments.table_name
                    )
                    try:
                        ensure_allowed_table_name(
                            arguments.table_name,
                            self._allowed_tables,
                        )
                    except ValueError as error:
                        tool_result = {"status": "failure", "result": str(error)}
                    else:
                        schema_result = await asyncio.to_thread(
                            self._schema_reader,
                            arguments.table_name,
                        )
                        schema_results.append(schema_result)
                        tool_result = schema_result.model_dump()
                elif tool_name == TABLE_DATA_INSPECTION_TOOL_NAME:
                    self._progress_reporter.entity_lookup_started(
                        arguments.table_name
                    )
                    if arguments.table_name not in self._allowed_tables:
                        inspection = TableDataInspectionResponse(
                            status="failure",
                            result=(
                                f"表 {arguments.table_name} 不属于当前查询业务域的允许范围"
                            ),
                        )
                    elif self._data_inspector is None:
                        inspection = TableDataInspectionResponse(
                            status="failure",
                            result="当前运行未配置单表数据检索子智能体。",
                        )
                    elif generation_count >= max_generation_count:
                        inspection = TableDataInspectionResponse(
                            status="failure",
                            result="已达到最大模型生成次数，无法启动单表数据检索子智能体。",
                        )
                    else:
                        inspection = await _resolve_maybe_awaitable(
                            self._data_inspector(
                                arguments.table_name,
                                arguments.request,
                                arguments.lookup_value,
                                generation_count + 1,
                                max_generation_count,
                                arguments.purpose,
                            )
                        )
                        generation_count += inspection.model_generation_count
                    data_inspections.append(inspection)
                    if inspection.status == "success" and inspection.inspection_id:
                        inspection_has_more_by_id[inspection.inspection_id] = (
                            inspection.has_more
                        )
                        if inspection.page_id:
                            inspection_page_tool_call_ids_by_id.setdefault(
                                inspection.inspection_id, {}
                            )[inspection.page_id] = tool_call.id
                    tool_result = inspection.render_for_planning()
                elif tool_name == NEXT_TABLE_DATA_INSPECTION_PAGE_TOOL_NAME:
                    if self._inspection_page_reader is None:
                        inspection = TableDataInspectionResponse(
                            status="failure",
                            result="当前运行未配置单表检索翻页能力。",
                        )
                    else:
                        inspection = await _resolve_maybe_awaitable(
                            self._inspection_page_reader(arguments.inspection_id)
                        )
                    data_inspections.append(inspection)
                    if inspection.status == "success" and inspection.inspection_id:
                        inspection_has_more_by_id[inspection.inspection_id] = (
                            inspection.has_more
                        )
                        if inspection.page_id:
                            inspection_page_tool_call_ids_by_id.setdefault(
                                inspection.inspection_id, {}
                            )[inspection.page_id] = tool_call.id
                    tool_result = inspection.render_for_planning()
                elif tool_name == CLEAR_TABLE_DATA_INSPECTION_CONTEXT_TOOL_NAME:
                    page_tool_call_ids = inspection_page_tool_call_ids_by_id.get(
                        arguments.inspection_id
                    )
                    if page_tool_call_ids is None:
                        tool_result = {
                            "status": "failure",
                            "result": "指定的单表检索结果不存在，无法清理上下文。",
                        }
                    elif unknown_page_ids := (
                        set(arguments.preserved_page_ids)
                        - set(page_tool_call_ids)
                    ):
                        tool_result = {
                            "status": "failure",
                            "result": "存在不属于该检索结果的关键页面，无法清理上下文。",
                            "unknown_page_ids": sorted(unknown_page_ids),
                        }
                    else:
                        cleared_count = _clear_inspection_page_messages(
                            messages,
                            page_tool_call_ids,
                            set(arguments.preserved_page_ids),
                            cleared_inspection_tool_call_ids,
                        )
                        decision_label = (
                            "已找到并确认"
                            if arguments.decision == "confirmed"
                            else "已排除"
                        )
                        tool_result = {
                            "status": "success",
                            "result": (
                                f"已清除 {cleared_count} 页无关候选内容；"
                                f"{decision_label}：{arguments.candidate_result}"
                            ),
                            "inspection_id": arguments.inspection_id,
                            "preserved_page_ids": arguments.preserved_page_ids,
                            "has_more": inspection_has_more_by_id[
                                arguments.inspection_id
                            ],
                        }
                elif tool_name == ASK_USER_TOOL_NAME:
                    interaction = UserInteraction(
                        question=arguments.question,
                        answer=await _resolve_maybe_awaitable(
                            self._user_input_reader(arguments.question)
                        ),
                    )
                    user_interactions.append(interaction)
                    tool_result = {"status": "success", "result": interaction.model_dump()}
                elif tool_name == QUERY_MATERIAL_DATA_TOOL_NAME:
                    unknown_tables = sorted(
                        set(arguments.required_tables) - self._allowed_tables
                    )
                    duplicate_tables = len(arguments.required_tables) != len(
                        set(arguments.required_tables)
                    )
                    if unknown_tables:
                        tool_result = _render_material_query_request_failure(
                            "所需数据表包含当前业务域不允许的表："
                            + "、".join(unknown_tables),
                            "删除这些表，并只使用当前业务域表概述中列出的真实表名。",
                        )
                    elif duplicate_tables:
                        tool_result = _render_material_query_request_failure(
                            "required_tables 中存在重复表名。",
                            "删除重复项，并保留各表首次出现的依赖顺序。",
                        )
                    elif self._material_query_runner is None:
                        tool_result = _render_material_query_request_failure(
                            "当前运行未配置原料查询能力。",
                            "这是运行配置问题，不能通过重复调用或修改查询口径解决；结束本次查询。",
                        )
                    else:
                        self._progress_reporter.material_query_started()
                        try:
                            sql_result = await _resolve_maybe_awaitable(
                                self._material_query_runner(arguments, schema_results)
                            )
                        except Exception as error:
                            self._write_trace(
                                "\n----- 原料查询工具内部异常 -----\n"
                                f"exception_type: {type(error).__name__}\n"
                                f"message: {str(error)[:500]}"
                            )
                            tool_result = _render_material_query_request_failure(
                                "原料查询服务本轮未能完成执行。",
                                "这是执行环境错误，不要原样重复调用；可稍后重试本次查询。",
                            )
                            self._progress_reporter.material_query_failed()
                        else:
                            if sql_result.status == "success":
                                if (
                                    sql_result.sql is None
                                    or sql_result.analysis_sql is None
                                    or not sql_result.result_columns
                                    or sql_result.returned_row_count
                                    != len(sql_result.rows)
                                ):
                                    tool_result = _render_material_query_request_failure(
                                        "原料查询返回了不完整的内部结果结构。",
                                        "这是执行器结果契约错误，不能通过修改查询指导解决；结束本次查询。",
                                    )
                                    self._progress_reporter.material_query_failed()
                                else:
                                    material_result_id = _create_planning_result_id(
                                        "material"
                                    )
                                    material_results[material_result_id] = (
                                        _MaterialResultEntry(
                                            arguments=arguments,
                                            sql_result=sql_result,
                                        )
                                    )
                                    tool_result = _render_material_query_success(
                                        material_result_id,
                                        sql_result,
                                    )
                                    self._progress_reporter.material_query_completed(
                                        sql_result.returned_row_count
                                    )
                            else:
                                tool_result = _render_material_query_failure(sql_result)
                                self._progress_reporter.material_query_failed()
                elif tool_name == SHAPE_MATERIAL_DATA_TOOL_NAME:
                    material_entry = material_results.get(
                        arguments.material_result_id
                    )
                    if material_entry is None:
                        available_ids = list(material_results)
                        available_text = (
                            "、".join(f"`{item}`" for item in available_ids)
                            if available_ids
                            else "当前还没有成功的原料结果"
                        )
                        tool_result = _render_material_shaping_request_failure(
                            arguments.material_result_id,
                            "指定的原料结果 ID 不存在或并非本轮成功结果。",
                            "只能使用 query_material_data 成功返回的 ID。"
                            f"当前可用结果：{available_text}。",
                        )
                    elif self._material_shaping_runner is None:
                        tool_result = _render_material_shaping_request_failure(
                            arguments.material_result_id,
                            "当前运行未配置原料塑形能力。",
                            "这是运行配置问题，不能通过修改塑形指导解决；结束本次查询。",
                        )
                    else:
                        self._progress_reporter.material_shaping_started()
                        try:
                            shaping_result = await _resolve_maybe_awaitable(
                                self._material_shaping_runner(
                                    arguments,
                                    material_entry.sql_result,
                                )
                            )
                        except Exception as error:
                            self._write_trace(
                                "\n----- 原料塑形工具内部异常 -----\n"
                                f"exception_type: {type(error).__name__}\n"
                                f"message: {str(error)[:500]}"
                            )
                            tool_result = _render_material_shaping_request_failure(
                                arguments.material_result_id,
                                "原料塑形服务本轮未能完成执行。",
                                "这是执行环境错误，不要原样重复调用；可稍后重试本次查询。",
                            )
                            self._progress_reporter.material_shaping_failed()
                        else:
                            if shaping_result.status == "success":
                                if (
                                    not shaping_result.columns
                                    or shaping_result.source_row_count
                                    != len(material_entry.sql_result.rows)
                                    or shaping_result.result_row_count
                                    != len(shaping_result.rows)
                                ):
                                    tool_result = (
                                        _render_material_shaping_request_failure(
                                            arguments.material_result_id,
                                            "塑形工具返回了不完整的内部结果结构。",
                                            "这是塑形器结果契约错误，不能通过修改布局指导解决；结束本次查询。",
                                        )
                                    )
                                    self._progress_reporter.material_shaping_failed()
                                else:
                                    shaped_result_id = _create_planning_result_id(
                                        "shaped"
                                    )
                                    shaped_results[shaped_result_id] = (
                                        _ShapedResultEntry(
                                            material_result_id=(
                                                arguments.material_result_id
                                            ),
                                            shaping_guidance=(
                                                arguments.shaping_guidance
                                            ),
                                            shaping_result=shaping_result,
                                        )
                                    )
                                    tool_result = _render_material_shaping_success(
                                        shaped_result_id,
                                        arguments.material_result_id,
                                        shaping_result,
                                    )
                                    self._progress_reporter.material_shaping_completed(
                                        shaping_result.source_row_count,
                                        shaping_result.result_row_count,
                                    )
                            else:
                                tool_result = _render_material_shaping_failure(
                                    arguments.material_result_id,
                                    shaping_result,
                                )
                                self._progress_reporter.material_shaping_failed()
                tool_content = (
                    tool_result
                    if isinstance(tool_result, str)
                    else render_yaml_context(tool_result)
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_content,
                    }
                )
                # 连续两次成功思考后，为下一轮临时追加温和的系统级动作指导。
                if (
                    tool_name == THINKING_TOOL_NAME
                    and consecutive_think_count >= 2
                ):
                    pending_system_guidance = build_system_guidance_message(
                        PLANNING_REPEATED_THINK_GUIDANCE
                    )
                    messages.append(pending_system_guidance)
                self._write_trace(
                    f"\n----- 第 {generation_index} 次模型调用的工具执行结果 -----\n"
                    f"tool_call_id: {tool_call.id}\n"
                    f"tool_name: {tool_name}\n"
                    f"result: {tool_content}"
                )
            if (
                not terminal_tool_calls
                and _resolves_planning_repair_origin(
                    tool_calls,
                    repair_origin_tool_names,
                )
                and _remove_repaired_planning_context(
                    messages,
                    repair_context_start,
                    current_turn_start,
                )
            ):
                repair_context_start = None
                repair_origin_tool_names = None
                self._write_trace(
                    "\n----- 查询规划修复上下文清理 -----\n"
                    "模型已修正协议或参数错误；此前无效调用及错误反馈已从后续上下文移除。"
                )
            if terminal_tool_calls:
                terminal_tool_call = terminal_tool_calls[0]
                if terminal_tool_call.function.name == ABANDON_QUERY_PLANNING_TOOL_NAME:
                    try:
                        abandonment = parse_abandon_query_planning_arguments(
                            terminal_tool_call.function.arguments
                        )
                    except ValidationError as error:
                        if repair_context_start is None:
                            repair_context_start = current_turn_start
                            repair_origin_tool_names = Counter(
                                {ABANDON_QUERY_PLANNING_TOOL_NAME: 1}
                            )
                        error_message = build_tool_argument_error_message(
                            terminal_tool_call.id,
                            ABANDON_QUERY_PLANNING_TOOL_NAME,
                            error,
                        )
                        self._write_trace(
                            "\n----- 查询规划终止工具参数校验结果 -----\n"
                            f"tool_call_id: {terminal_tool_call.id}\n"
                            f"tool_name: {ABANDON_QUERY_PLANNING_TOOL_NAME}\n"
                            f"result: {error_message['content']}"
                        )
                        messages.append(error_message)
                        continue
                    _remove_repaired_planning_context(
                        messages,
                        repair_context_start,
                        current_turn_start,
                    )
                    return {
                        "result": QueryPlanningAgentResult(
                            status="abandoned",
                            abandonment=abandonment,
                            thoughts=thoughts,
                            schema_results=schema_results,
                            data_inspections=data_inspections,
                            user_interactions=user_interactions,
                            raw_responses=raw_responses,
                            generation_count=generation_count,
                            max_generation_count=max_generation_count,
                            tool_call_count=tool_call_count,
                            max_tool_call_count=max_tool_call_count,
                        )
                    }
            if final_result_call is not None:
                try:
                    final_selection = parse_submit_final_query_result_tool_arguments(
                        final_result_call.function.arguments
                    )
                except ValidationError as error:
                    if repair_context_start is None:
                        repair_context_start = current_turn_start
                        repair_origin_tool_names = Counter(
                            {SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME: 1}
                        )
                    error_message = build_tool_argument_error_message(
                        final_result_call.id,
                        SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME,
                        error,
                    )
                    self._write_trace(
                        "\n----- 查询规划终止工具参数校验结果 -----\n"
                        f"tool_call_id: {final_result_call.id}\n"
                        f"tool_name: {SUBMIT_FINAL_QUERY_RESULT_TOOL_NAME}\n"
                        f"result: {error_message['content']}"
                    )
                    messages.append(error_message)
                    continue

                shaped_entry = shaped_results.get(final_selection.shaped_result_id)
                if shaped_entry is None:
                    reference_failure = _render_final_result_reference_failure(
                        final_selection.shaped_result_id,
                        list(shaped_results),
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": final_result_call.id,
                            "content": reference_failure,
                        }
                    )
                    self._write_trace(
                        "\n----- 查询规划最终结果引用校验 -----\n"
                        f"tool_call_id: {final_result_call.id}\n"
                        f"result: {reference_failure}"
                    )
                    continue

                material_entry = material_results.get(
                    shaped_entry.material_result_id
                )
                if material_entry is None:
                    reference_failure = _render_final_result_reference_failure(
                        final_selection.shaped_result_id,
                        [],
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": final_result_call.id,
                            "content": reference_failure,
                        }
                    )
                    self._write_trace(
                        "\n----- 查询规划最终结果内部引用校验 -----\n"
                        f"tool_call_id: {final_result_call.id}\n"
                        "result: 塑形结果对应的原料缓存不存在"
                    )
                    continue

                if _remove_repaired_planning_context(
                    messages,
                    repair_context_start,
                    current_turn_start,
                ):
                    repair_context_start = None
                    repair_origin_tool_names = None
                    self._write_trace(
                        "\n----- 查询规划修复上下文清理 -----\n"
                        "终止工具已按错误反馈修正；此前无效调用及反馈已从上下文移除。"
                    )
                review_question = _build_final_result_review_question(
                    shaped_entry.shaping_result,
                )
                review_interaction = UserInteraction(
                    question=review_question,
                    answer=await _resolve_maybe_awaitable(
                        self._plan_review_reader(review_question)
                    ),
                )
                user_interactions.append(review_interaction)
                if _is_query_plan_review_cancelled(review_interaction.answer):
                    return {
                        "result": QueryPlanningAgentResult(
                            status="abandoned",
                            abandonment=QueryPlanningAbandonment(
                                reason_type="user_cancelled",
                                user_message="用户取消了本次查询。",
                                confirmed_facts=[],
                            ),
                            thoughts=thoughts,
                            schema_results=schema_results,
                            data_inspections=data_inspections,
                            user_interactions=user_interactions,
                            raw_responses=raw_responses,
                            generation_count=generation_count,
                            max_generation_count=max_generation_count,
                            tool_call_count=tool_call_count,
                            max_tool_call_count=max_tool_call_count,
                        )
                    }
                if not _is_query_plan_review_approved(review_interaction.answer):
                    revision_feedback = review_interaction.answer
                    if _is_query_plan_revision_requested(review_interaction.answer):
                        revision_interaction = UserInteraction(
                            question=PLAN_REVISION_QUESTION,
                            answer=await _resolve_maybe_awaitable(
                                self._user_input_reader(PLAN_REVISION_QUESTION)
                            ),
                        )
                        user_interactions.append(revision_interaction)
                        revision_feedback = revision_interaction.answer
                    revision_message = _build_query_plan_revision_message(
                        final_result_call.id,
                        review_question,
                        revision_feedback,
                    )
                    messages.append(revision_message)
                    self._progress_reporter.plan_revision_started()
                    self._write_trace(
                        "\n----- 查询规划用户复核结果 -----\n"
                        f"tool_call_id: {final_result_call.id}\n"
                        "result: 用户要求修订已生成的最终表格"
                    )
                    continue

                material_plan = MaterialQueryPlan(
                    guidance=material_entry.arguments.guidance,
                    required_tables=material_entry.arguments.required_tables,
                    raw_material_shaping_guidance=(
                        shaped_entry.shaping_guidance
                    ),
                )
                final_result = PlanningFinalQueryResult(
                    material_result_id=shaped_entry.material_result_id,
                    shaped_result_id=final_selection.shaped_result_id,
                    selection_reason=final_selection.reason,
                    material_plan=material_plan,
                    sql_result=material_entry.sql_result,
                    shaping_result=shaped_entry.shaping_result,
                )
                return {
                    "result": QueryPlanningAgentResult(
                        status="success",
                        thoughts=thoughts,
                        schema_results=schema_results,
                        data_inspections=data_inspections,
                        user_interactions=user_interactions,
                        material_plan=material_plan,
                        final_result=final_result,
                        query_request=render_material_query_plan(material_plan),
                        raw_responses=raw_responses,
                        generation_count=generation_count,
                        max_generation_count=max_generation_count,
                        tool_call_count=tool_call_count,
                        max_tool_call_count=max_tool_call_count,
                    )
                }
        raise QueryPlanningExecutionError(
            f"模型生成次数超过最大限制：{max_generation_count}",
            raw_responses,
        )

    # 异步执行完整工具循环，并分别限制模型生成和工具调用次数以控制成本及循环风险。
    async def run(
        self,
        user_question: str,
        max_generation_count: int = DEFAULT_MAX_GENERATION_COUNT,
        max_tool_call_count: int = DEFAULT_MAX_TOOL_CALL_COUNT,
    ) -> QueryPlanningAgentResult:
        normalized_question = user_question.strip()
        if not normalized_question:
            raise ValueError("用户问题不能为空")
        if (
            isinstance(max_generation_count, bool)
            or not isinstance(max_generation_count, int)
            or max_generation_count < 1
        ):
            raise ValueError("最大模型生成次数必须是大于零的整数")
        if (
            isinstance(max_tool_call_count, bool)
            or not isinstance(max_tool_call_count, int)
            or max_tool_call_count < 1
        ):
            raise ValueError("最大工具调用次数必须是大于零的整数")
        try:
            state = await self._workflow.ainvoke(
                {
                    "user_question": normalized_question,
                    "max_generation_count": max_generation_count,
                    "max_tool_call_count": max_tool_call_count,
                }
            )
        finally:
            if self._close_client_after_run:
                await self._client.close()
        return state["result"]
