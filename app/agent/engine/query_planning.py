"""使用 LangGraph 编排 DeepSeek 思考、表结构查询与联合查询自然语言生成。"""

import json
import re
from collections import Counter
from collections.abc import Callable
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent.domains.base import QueryDomainProfile, QueryPlanPolicyIssue
from app.agent.events.publisher import AgentProgressReporter, ProgressEmitter
from app.core.config import Settings, get_settings
from app.agent.runtime.model_options import (
    DEFAULT_PLANNING_MAX_TOKENS,
    build_non_thinking_completion_options,
    build_strict_tools_base_url,
)
from app.agent.tools.ask_user import (
    ASK_USER_TOOL_NAME,
    UserInteraction,
    build_ask_user_tool_definition,
    parse_ask_user_tool_arguments,
)
from app.agent.runtime.table_inspector import SingleTableDataInspector
from app.agent.tools.table_inspection import (
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
from app.agent.tools.query_plan import (
    ABANDON_QUERY_PLANNING_TOOL_NAME,
    NATURAL_LANGUAGE_QUERY_TOOL_NAME,
    NaturalLanguageQueryPlan,
    QueryPlanningAbandonment,
    ResultShapePlan,
    build_abandon_query_planning_tool_definition,
    build_natural_language_query_tool_definition,
    parse_abandon_query_planning_arguments,
    parse_natural_language_query_tool_arguments,
    render_natural_language_query_plan,
)
from app.agent.engine.planning_prompt import build_base_planning_prompt
from app.agent.runtime.table_schema_reader import InformationSchemaTableSchemaReader
from app.agent.runtime.table_schema_cache import CachingTableSchemaReader
from app.agent.runtime.yaml_context import parse_yaml_context, render_yaml_context
from app.agent.tools.table_schema import (
    TABLE_SCHEMA_TOOL_NAME,
    TableSchemaToolResponse,
    build_table_schema_tool_definition,
    ensure_allowed_table_name,
    parse_table_schema_tool_arguments,
)
from app.agent.tools.thinking import (
    THINKING_TOOL_NAME,
    ThinkingToolArguments,
    build_thinking_tool_definition,
    parse_thinking_tool_arguments,
)
from app.agent.tools.argument_feedback import (
    build_tool_argument_error_message,
    build_tool_policy_error_message,
)

DEFAULT_MAX_GENERATION_COUNT = 6
DEFAULT_MAX_TOOL_CALL_COUNT = 12
MAX_TERMINAL_ARGUMENT_REPAIR_COUNT = 5
SchemaReader = Callable[[str], TableSchemaToolResponse]
UserInputReader = Callable[[str], str]
TraceWriter = Callable[[str], None]
DataInspector = Callable[
    [str, str, str, int, int, DataInspectionPurpose], TableDataInspectionResponse
]
InspectionPageReader = Callable[[str], TableDataInspectionResponse]


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
            for condition in query_plan.quantified_conditions
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
                    field_path="query_plan.quantified_conditions",
                    message=(
                        "查询计划遗漏业务对齐层确认的集合量化条件："
                        + "、".join(missing_descriptions)
                    ),
                    repair_action=(
                        "为上述每个量词新增一条 quantified_conditions 项，"
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
                        "用 plan_references 指向实际落实该规则的 filters、joins、"
                        "quantified_conditions、having 或其他已有计划组件。"
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

    for index, condition in enumerate(query_plan.quantified_conditions):
        normalized_predicate = re.sub(
            r"\s+",
            "",
            condition.predicate.replace("`", "").lower(),
        )
        matching_filter_indexes = [
            filter_index
            for filter_index, query_filter in enumerate(query_plan.filters)
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
                        "query_plan.filters["
                        + ",".join(str(item) for item in matching_filter_indexes)
                        + "]"
                    ),
                    message=(
                        f"量词 {condition.quantifier} 的成员谓词被同时写成普通筛选，"
                        "这会在量化判断前删除反例，使全部或没有条件失真。"
                    ),
                    repair_action=(
                        "从 query_plan.filters 删除与该 quantified_conditions.predicate "
                        "相同的普通筛选；filters 只保留集合范围条件，成员谓词仅在 "
                        "NOT EXISTS、条件聚合、子查询或 CTE 的资格判断中计算。"
                    ),
                )
            )
        if (
            result_shape_plan.shape_type == "pivot"
            and condition.quantifier in {"all", "none"}
            and condition.implementation_hint != "not_exists"
        ):
            issues.append(
                QueryPlanPolicyIssue(
                    field_path=(
                        f"query_plan.quantified_conditions[{index}].implementation_hint"
                    ),
                    message=(
                        "最终结果需要保留集合成员逐行供 pivot 展开；当前全称或否定"
                        "量词没有使用可在外层保留成员行的 NOT EXISTS 资格判断。"
                    ),
                    repair_action=(
                        "把 implementation_hint 精确改为 not_exists：all 在相关子查询中"
                        "排除不满足 predicate 的反例成员，none 在相关子查询中排除满足"
                        " predicate 的成员；外层继续返回合格主体的全部集合成员供塑形。"
                    ),
                )
            )
        if condition.implementation_hint == "having" and not query_plan.having:
            issues.append(
                QueryPlanPolicyIssue(
                    field_path=f"query_plan.quantified_conditions[{index}]",
                    message="该量化条件声明使用 having，但 query_plan.having 为空。",
                    repair_action=(
                        "在 query_plan.having 中加入落实该量化条件的聚合后筛选表达式；"
                        "如果实际不用 HAVING，则把 implementation_hint 改为真实实现方式。"
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
                        "在 query_plan.select_fields 中加入最终行主体真实表的 id 字段，"
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
                        "这些字段继续保留在 group_fields 和 query_plan.select_fields 中。"
                    ),
                )
            )
    return tuple(issues)


class QueryPlanningAgentResult(BaseModel):
    """一次查询规划产生的关键判断、结构读取结果和最终联合查询自然语言。"""

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
    user_interactions: list[UserInteraction] = Field(description="模型通过交互出口发起的澄清问答")
    query_plan: NaturalLanguageQueryPlan | None = Field(
        default=None, description="成功时只供 SQL 查询执行层消费的数据获取计划"
    )
    result_shape_plan: ResultShapePlan | None = Field(
        default=None, description="成功时只供翻译后本地塑形层消费的确定性计划"
    )
    query_request: str | None = Field(
        default=None, description="成功时可交给后续 SQL 执行器的联合查询自然语言"
    )
    raw_responses: list[str] = Field(description="每轮工具调用的原始模型响应")
    generation_count: int = Field(description="本次规划实际发起的模型生成次数")
    max_generation_count: int = Field(description="本次规划允许的最大模型生成次数")
    tool_call_count: int = Field(description="本次规划实际处理的工具调用次数")
    max_tool_call_count: int = Field(description="本次规划允许的最大工具调用次数")

    # 保证放弃终态没有可执行查询计划，防止下游 SQL 子图在业务无法继续时被意外调用。
    @model_validator(mode="after")
    def validate_terminal_payload(self) -> "QueryPlanningAgentResult":
        if self.status == "success":
            if self.result_shape_plan is None and self.query_plan is not None:
                self.result_shape_plan = ResultShapePlan(
                    passthrough_fields=[
                        field.result_field for field in self.query_plan.select_fields
                    ]
                )
            if (
                self.query_plan is None
                or self.result_shape_plan is None
                or self.query_request is None
                or self.abandonment is not None
            ):
                raise ValueError("查询规划成功结果必须且只能包含查询计划")
        elif (
            self.abandonment is None
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
def _raise_missing_user_interaction(_: str) -> str:
    raise RuntimeError("查询规划需要用户回答，但未配置用户交互出口")


# 按规划轮次强制工具协议：首轮固定记录关键判断，后续只能以任一注册工具继续推进。
def _build_planning_tool_choice(generation_count: int) -> str | dict[str, object]:
    if generation_count == 1:
        return {
            "type": "function",
            "function": {"name": THINKING_TOOL_NAME},
        }
    return "required"


class DeepSeekQueryPlanningAgent:
    """在工具循环中按需读取表结构，直到生成最终联合查询自然语言。"""

    # 初始化模型、表概览、结构读取器及 LangGraph 工作流，便于测试替换外部依赖。
    def __init__(
        self,
        client: Any,
        model: str,
        domain_profile: QueryDomainProfile,
        schema_reader: SchemaReader,
        user_input_reader: UserInputReader = _raise_missing_user_interaction,
        trace_writer: TraceWriter | None = None,
        data_inspector: DataInspector | None = None,
        inspection_page_reader: InspectionPageReader | None = None,
        progress_emitter: ProgressEmitter | None = None,
        max_tokens: int = DEFAULT_PLANNING_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model = model
        self._domain_profile = domain_profile
        self._allowed_tables = frozenset(domain_profile.allowed_tables)
        self._schema_reader = schema_reader
        self._user_input_reader = user_input_reader
        self._trace_writer = trace_writer
        self._data_inspector = data_inspector
        self._inspection_page_reader = inspection_page_reader
        self._progress_reporter = AgentProgressReporter(
            domain_profile,
            progress_emitter,
        )
        self._max_tokens = max_tokens
        self._base_prompt = build_base_planning_prompt(domain_profile)

        workflow = StateGraph(_QueryAgentState)
        workflow.add_node("run_tool_loop", self._run_tool_loop)
        workflow.add_edge(START, "run_tool_loop")
        workflow.add_edge("run_tool_loop", END)
        self._workflow = workflow.compile()

    # 使用应用配置创建真实 DeepSeek 客户端，并让规划工具与单表检索共享同一结构读取器。
    @classmethod
    def from_settings(
        cls,
        domain_profile: QueryDomainProfile,
        settings: Settings | None = None,
        schema_reader: SchemaReader | None = None,
        user_input_reader: UserInputReader = _raise_missing_user_interaction,
        trace_writer: TraceWriter | None = None,
        progress_emitter: ProgressEmitter | None = None,
    ) -> "DeepSeekQueryPlanningAgent":
        resolved_settings = settings or get_settings()
        if resolved_settings.deepseek_api_key is None:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法执行查询规划")
        client = OpenAI(
            api_key=resolved_settings.deepseek_api_key.get_secret_value(),
            base_url=build_strict_tools_base_url(str(resolved_settings.deepseek_base_url)),
            timeout=resolved_settings.deepseek_http_timeout_seconds,
        )
        schema_reader = schema_reader or CachingTableSchemaReader(
            InformationSchemaTableSchemaReader(
                resolved_settings,
                domain_profile.allowed_tables,
            ).read
        ).read
        data_inspector = SingleTableDataInspector(
            client=client,
            model=resolved_settings.deepseek_model,
            settings=resolved_settings,
            domain_profile=domain_profile,
            schema_reader=schema_reader,
            trace_writer=trace_writer,
            max_tokens=resolved_settings.deepseek_query_inspection_max_tokens,
        )
        return cls(
            client=client,
            model=resolved_settings.deepseek_model,
            domain_profile=domain_profile,
            schema_reader=schema_reader,
            user_input_reader=user_input_reader,
            trace_writer=trace_writer,
            data_inspector=data_inspector.inspect,
            inspection_page_reader=data_inspector.get_next_page,
            progress_emitter=progress_emitter,
            max_tokens=resolved_settings.deepseek_query_planning_max_tokens,
        )

    # 在显式启用内部追踪时记录模型、工具和执行结果，默认不向标准输出泄漏技术轨迹。
    def _write_trace(self, content: str) -> None:
        if self._trace_writer is not None:
            self._trace_writer(content)

    # 运行模型工具循环：首轮由 Prompt 约束为思考，支持同轮并行工具调用并逐项按 ID 回传结果。
    def _run_tool_loop(self, state: _QueryAgentState) -> dict[str, QueryPlanningAgentResult]:
        messages: list[Any] = [
            {"role": "system", "content": self._base_prompt},
            {"role": "user", "content": state["user_question"]},
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
            build_natural_language_query_tool_definition(),
            build_abandon_query_planning_tool_definition(
                self._domain_profile.query_scope
            ),
        ]
        thoughts: list[ThinkingToolArguments] = []
        schema_results: list[TableSchemaToolResponse] = []
        data_inspections: list[TableDataInspectionResponse] = []
        user_interactions: list[UserInteraction] = []
        raw_responses: list[str] = []
        terminal_argument_repair_count = 0
        inspection_page_tool_call_ids_by_id: dict[str, dict[str, str]] = {}
        inspection_has_more_by_id: dict[str, bool] = {}
        cleared_inspection_tool_call_ids: set[str] = set()
        max_generation_count = state["max_generation_count"]
        max_tool_call_count = state["max_tool_call_count"]

        tool_call_count = 0
        generation_count = 0
        while generation_count < max_generation_count:
            generation_count += 1
            generation_index = generation_count
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=tools,
                tool_choice=_build_planning_tool_choice(generation_count),
                **build_non_thinking_completion_options(self._max_tokens),
            )
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
                content = getattr(message, "content", None) or ""
                raise QueryPlanningExecutionError(
                    "DeepSeek 未调用查询工具，无法继续工作流。"
                    f"模型文本：{content[:200]}",
                    raw_responses,
                )
            if tool_call_count + len(tool_calls) > max_tool_call_count:
                raise QueryPlanningExecutionError(
                    f"工具调用超过最大次数：{max_tool_call_count}",
                    raw_responses,
                )
            tool_call_count += len(tool_calls)

            messages.append(message)
            terminal_tool_calls = [
                tool_call
                for tool_call in tool_calls
                if tool_call.function.name
                in {
                    NATURAL_LANGUAGE_QUERY_TOOL_NAME,
                    ABANDON_QUERY_PLANNING_TOOL_NAME,
                }
            ]
            if len(terminal_tool_calls) > 1:
                raise QueryPlanningExecutionError(
                    "同一轮只能调用一次终止工具",
                    raw_responses,
                )
            if terminal_tool_calls and any(
                tool_call.function.name == ASK_USER_TOOL_NAME
                for tool_call in tool_calls
            ):
                raise QueryPlanningExecutionError(
                    "终止工具不能与 ask_user 在同一轮调用",
                    raw_responses,
                )

            final_query_call = next(
                (
                    tool_call
                    for tool_call in tool_calls
                    if tool_call.function.name == NATURAL_LANGUAGE_QUERY_TOOL_NAME
                ),
                None,
            )
            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                if tool_name in {
                    NATURAL_LANGUAGE_QUERY_TOOL_NAME,
                    ABANDON_QUERY_PLANNING_TOOL_NAME,
                }:
                    continue
                try:
                    arguments = _parse_nonterminal_tool_arguments(
                        tool_name,
                        tool_call.function.arguments,
                    )
                except KeyError:
                    raise QueryPlanningExecutionError(
                        f"DeepSeek 调用了未注册工具：{tool_name}",
                        raw_responses,
                    ) from None
                except ValidationError as error:
                    error_message = build_tool_argument_error_message(
                        tool_call.id,
                        tool_name,
                        error,
                    )
                    messages.append(error_message)
                    self._write_trace(
                        f"\n----- 第 {generation_index} 次模型调用的工具参数校验结果 -----\n"
                        f"tool_call_id: {tool_call.id}\n"
                        f"tool_name: {tool_name}\n"
                        f"result: {error_message['content']}"
                    )
                    continue

                if tool_name == THINKING_TOOL_NAME:
                    thought = arguments
                    thoughts.append(thought)
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
                        schema_result = self._schema_reader(arguments.table_name)
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
                        inspection = self._data_inspector(
                            arguments.table_name,
                            arguments.request,
                            arguments.lookup_value,
                            generation_count + 1,
                            max_generation_count,
                            arguments.purpose,
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
                        inspection = self._inspection_page_reader(arguments.inspection_id)
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
                        answer=self._user_input_reader(arguments.question),
                    )
                    user_interactions.append(interaction)
                    tool_result = {"status": "success", "result": interaction.model_dump()}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": render_yaml_context(tool_result),
                    }
                )
                self._write_trace(
                    f"\n----- 第 {generation_index} 次模型调用的工具执行结果 -----\n"
                    f"tool_call_id: {tool_call.id}\n"
                    f"tool_name: {tool_name}\n"
                    f"result: {json.dumps(tool_result, ensure_ascii=False, indent=2)}"
                )
            if terminal_tool_calls:
                terminal_tool_call = terminal_tool_calls[0]
                if terminal_tool_call.function.name == ABANDON_QUERY_PLANNING_TOOL_NAME:
                    try:
                        abandonment = parse_abandon_query_planning_arguments(
                            terminal_tool_call.function.arguments
                        )
                    except ValidationError as error:
                        if terminal_argument_repair_count < MAX_TERMINAL_ARGUMENT_REPAIR_COUNT:
                            terminal_argument_repair_count += 1
                            error_message = build_tool_argument_error_message(
                                terminal_tool_call.id,
                                ABANDON_QUERY_PLANNING_TOOL_NAME,
                                error,
                            )
                            messages.append(error_message)
                            self._write_trace(
                                "\n----- 查询规划终止工具参数校验结果 -----\n"
                                f"tool_call_id: {terminal_tool_call.id}\n"
                                f"tool_name: {ABANDON_QUERY_PLANNING_TOOL_NAME}\n"
                                f"result: {error_message['content']}"
                            )
                            continue
                        raise QueryPlanningExecutionError(
                            "查询规划放弃工具参数不符合约束",
                            raw_responses,
                        ) from error
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
            if final_query_call is not None:
                try:
                    query_arguments = parse_natural_language_query_tool_arguments(
                        final_query_call.function.arguments
                    )
                except ValidationError as error:
                    if terminal_argument_repair_count < MAX_TERMINAL_ARGUMENT_REPAIR_COUNT:
                        terminal_argument_repair_count += 1
                        error_message = build_tool_argument_error_message(
                            final_query_call.id,
                            NATURAL_LANGUAGE_QUERY_TOOL_NAME,
                            error,
                        )
                        messages.append(error_message)
                        self._write_trace(
                            "\n----- 查询规划终止工具参数校验结果 -----\n"
                            f"tool_call_id: {final_query_call.id}\n"
                            f"tool_name: {NATURAL_LANGUAGE_QUERY_TOOL_NAME}\n"
                            f"result: {error_message['content']}"
                        )
                        continue
                    raise QueryPlanningExecutionError(
                        "查询规划最终工具参数不符合约束",
                        raw_responses,
                    ) from error
                policy_issues = (
                    _validate_query_plan_contract(
                        state["user_question"],
                        query_arguments.query_plan,
                        query_arguments.result_shape_plan,
                    )
                    + self._domain_profile.validate_query_plan(
                        state["user_question"],
                        query_arguments.query_plan,
                    )
                )
                if policy_issues:
                    if terminal_argument_repair_count < MAX_TERMINAL_ARGUMENT_REPAIR_COUNT:
                        terminal_argument_repair_count += 1
                        error_message = build_tool_policy_error_message(
                            final_query_call.id,
                            NATURAL_LANGUAGE_QUERY_TOOL_NAME,
                            policy_issues,
                        )
                        messages.append(error_message)
                        self._write_trace(
                            "\n----- 查询规划业务规则校验结果 -----\n"
                            f"tool_call_id: {final_query_call.id}\n"
                            f"tool_name: {NATURAL_LANGUAGE_QUERY_TOOL_NAME}\n"
                            f"result: {error_message['content']}"
                        )
                        continue
                    raise QueryPlanningExecutionError(
                        "查询规划最终工具参数连续违反业务域规则",
                        raw_responses,
                    )
                return {
                    "result": QueryPlanningAgentResult(
                        status="success",
                        thoughts=thoughts,
                        schema_results=schema_results,
                        data_inspections=data_inspections,
                        user_interactions=user_interactions,
                        query_plan=query_arguments.query_plan,
                        result_shape_plan=query_arguments.result_shape_plan,
                        query_request=render_natural_language_query_plan(
                            query_arguments.query_plan,
                            query_arguments.result_shape_plan,
                        ),
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

    # 执行完整工具循环，并分别限制模型生成和工具调用次数以控制成本及循环风险。
    def run(
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
        state = self._workflow.invoke(
            {
                "user_question": normalized_question,
                "max_generation_count": max_generation_count,
                "max_tool_call_count": max_tool_call_count,
            }
        )
        return state["result"]
