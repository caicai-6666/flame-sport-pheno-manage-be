"""定义查询结果确定性塑形子图的状态、节点及运行逻辑。"""

import json
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Final, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.text2sql.domains.base import QueryDomainProfile
from app.agent.text2sql.model_messages import (
    ModelMessageTraceQueue,
    create_traced_chat_completion,
)
from app.agent.text2sql.shared.model_options import (
    DEFAULT_SHAPING_MAX_TOKENS,
    get_model_request_profile,
    resolve_model_provider_connection,
)
from app.agent.text2sql.subgraphs.shaping.result_shape_contract import (
    DynamicColumnContract,
    DynamicColumnContractError,
    parse_dynamic_column_contract,
)
from app.agent.text2sql.shared.tool_tag_template import (
    load_tool_tag_template,
    resolve_query_tool_tag_template_filename,
)
from app.agent.text2sql.function_calling.feedback import (
    build_tool_argument_error_message,
)
from app.agent.text2sql.subgraphs.planning.tools.query_plan import (
    NaturalLanguageQueryPlan,
    ResultShapePlan,
)
from app.agent.text2sql.subgraphs.shaping.models import MaterialResultShapePlan
from app.agent.text2sql.subgraphs.shaping.prompt import (
    build_material_shaping_messages,
)
from app.agent.text2sql.subgraphs.shaping.tool import (
    SUBMIT_MATERIAL_SHAPE_PLAN_TOOL_NAME,
    build_material_shape_plan_tool_definition,
    parse_material_shape_plan_tool_arguments,
)
from app.core.config import Settings, get_settings


MATERIAL_SHAPING_PREVIEW_ROW_COUNT: Final[int] = 5
MAX_MATERIAL_SHAPING_REPAIR_COUNT: Final[int] = 1
TraceWriter = Callable[[str], None]


class ShapedResultColumn(BaseModel):
    """塑形后一个稳定机器列名及其前端展示标题。"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="塑形后行对象使用的稳定列键")
    label: str = Field(description="供前端展示的人类可读列标题")
    source_result_field: str | None = Field(
        default=None,
        description="该展示列直接继承的 SQL 结果列，用于塑形后的字段翻译",
    )


class ResultShapingSubgraphResult(BaseModel):
    """确定性塑形子图的结果，失败时不返回部分塑形数据。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "failure"] = Field(description="塑形执行状态")
    columns: list[ShapedResultColumn] = Field(
        default_factory=list,
        description="塑形后的列定义",
    )
    rows: list[dict[str, Any]] = Field(
        default_factory=list,
        description="塑形后的完整结果行",
    )
    source_row_count: int = Field(
        default=0,
        description="塑形前 SQL 结果行数",
    )
    result_row_count: int = Field(
        default=0,
        description="塑形后的最终结果行数",
    )
    error: str | None = Field(default=None, description="失败时的可定位原因")
    material_shape_plan: MaterialResultShapePlan | None = Field(
        default=None,
        description="新原料协议编译出的确定性塑形计划",
    )
    raw_model_responses: list[str] = Field(
        default_factory=list,
        description="塑形计划生成模型的原始响应，仅供受限诊断回放",
    )


class _ResultShapingState(TypedDict, total=False):
    """塑形子图在输入与确定性执行节点之间传递的状态。"""

    query_plan: NaturalLanguageQueryPlan
    shape_plan: ResultShapePlan
    rows: list[dict[str, Any]]
    result: ResultShapingSubgraphResult


class _MaterialResultShapingState(TypedDict, total=False):
    """新原料塑形子图在布局编译与确定性执行间传递的最小状态。"""

    shaping_guidance: str
    result_columns: list[str]
    rows: list[dict[str, Any]]
    shape_plan: MaterialResultShapePlan
    raw_model_responses: list[str]
    result: ResultShapingSubgraphResult


class MaterialShapePlanValidationError(ValueError):
    """携带稳定错误代码和唯一修复动作的塑形计划语义异常。"""

    # 保存可直接回传模型的精确错误事实，避免用同一条模糊建议处理不同字段问题。
    def __init__(self, code: str, message: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.repair_action = repair_action


# 根据 SQL 计划的稳定结果列生成展示标签，动态列标签由塑形计划另行产生。
def _build_source_labels(query_plan: NaturalLanguageQueryPlan) -> dict[str, str]:
    return {
        select_field.result_field: select_field.purpose
        for select_field in query_plan.select_fields
    }


# 将可能包含空值或混合类型的组内排序值转换为可稳定比较的元组。
def _build_sort_key(value: Any) -> tuple[int, str, str]:
    if value is None:
        return (1, "", "")
    return (0, type(value).__name__, str(value))


# 校验塑形计划所需列都存在于 SQL 计划，运行时不依赖首行数据猜测列结构。
def _validate_required_fields(
    query_plan: NaturalLanguageQueryPlan,
    shape_plan: ResultShapePlan,
) -> None:
    available_fields = {
        select_field.result_field for select_field in query_plan.select_fields
    }
    required_fields = set(shape_plan.group_fields)
    required_fields.update(shape_plan.passthrough_fields)
    required_fields.update(shape_plan.hidden_fields)
    if shape_plan.pivot_value_field is not None:
        required_fields.add(shape_plan.pivot_value_field)
    if shape_plan.pivot_order_field is not None:
        required_fields.add(shape_plan.pivot_order_field)
    unknown_fields = sorted(required_fields - available_fields)
    if unknown_fields:
        raise ValueError(
            "塑形计划引用了 SQL 结果中不存在的列：" + ", ".join(unknown_fields)
        )


# 按计划列顺序透传完整结果，同时移除不应展示的技术字段。
def _shape_passthrough_rows(
    query_plan: NaturalLanguageQueryPlan,
    shape_plan: ResultShapePlan,
    rows: list[dict[str, Any]],
) -> ResultShapingSubgraphResult:
    source_labels = _build_source_labels(query_plan)
    field_order = shape_plan.passthrough_fields or [
        select_field.result_field for select_field in query_plan.select_fields
    ]
    columns = [
        ShapedResultColumn(
            key=field_name,
            label=source_labels[field_name],
            source_result_field=field_name,
        )
        for field_name in field_order
    ]
    shaped_rows = [
        {field_name: row.get(field_name) for field_name in field_order}
        for row in rows
    ]
    return ResultShapingSubgraphResult(
        status="success",
        columns=columns,
        rows=shaped_rows,
        source_row_count=len(rows),
        result_row_count=len(shaped_rows),
    )


# 按稳定分组键聚合原始行，并拒绝同组透传字段出现互相矛盾的值。
def _group_pivot_rows(
    shape_plan: ResultShapePlan,
    rows: list[dict[str, Any]],
) -> OrderedDict[tuple[Any, ...], list[dict[str, Any]]]:
    grouped_rows: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        group_key = tuple(row.get(field) for field in shape_plan.group_fields)
        grouped_rows.setdefault(group_key, []).append(row)
    for group_rows in grouped_rows.values():
        for field_name in shape_plan.passthrough_fields:
            first_value = group_rows[0].get(field_name)
            if any(row.get(field_name) != first_value for row in group_rows[1:]):
                raise ValueError(
                    f"同一塑形分组内的透传字段 {field_name} 存在多个不同值"
                )
    return grouped_rows


# 将同一主体的多行对象按序展开为动态列，列数量由完整结果和可选业务上限共同决定。
def _shape_pivot_rows(
    query_plan: NaturalLanguageQueryPlan,
    shape_plan: ResultShapePlan,
    rows: list[dict[str, Any]],
) -> ResultShapingSubgraphResult:
    assert shape_plan.pivot_value_field is not None
    assert shape_plan.column_key_prefix is not None
    assert shape_plan.column_label_pattern is not None
    source_labels = _build_source_labels(query_plan)
    grouped_rows = _group_pivot_rows(shape_plan, rows)
    maximum_group_size = max((len(group) for group in grouped_rows.values()), default=0)
    pivot_column_count = shape_plan.expected_pivot_columns or maximum_group_size
    if maximum_group_size > pivot_column_count:
        raise ValueError(
            "实际分组中的动态列数量超过 result_shape_plan.expected_pivot_columns"
        )

    dynamic_keys = [
        f"{shape_plan.column_key_prefix}_{index}"
        for index in range(1, pivot_column_count + 1)
    ]
    duplicate_keys = sorted(set(dynamic_keys) & set(shape_plan.passthrough_fields))
    if duplicate_keys:
        raise ValueError("动态列键与透传字段冲突：" + ", ".join(duplicate_keys))
    columns = [
        ShapedResultColumn(
            key=field_name,
            label=source_labels[field_name],
            source_result_field=field_name,
        )
        for field_name in shape_plan.passthrough_fields
    ] + [
        ShapedResultColumn(
            key=dynamic_key,
            label=shape_plan.column_label_pattern.format(index=index),
            source_result_field=shape_plan.pivot_value_field,
        )
        for index, dynamic_key in enumerate(dynamic_keys, start=1)
    ]

    shaped_rows: list[dict[str, Any]] = []
    for group_rows in grouped_rows.values():
        ordered_rows = list(group_rows)
        if shape_plan.pivot_order_field is not None:
            ordered_rows.sort(
                key=lambda row: _build_sort_key(
                    row.get(shape_plan.pivot_order_field)
                )
            )
        shaped_row = {
            field_name: ordered_rows[0].get(field_name)
            for field_name in shape_plan.passthrough_fields
        }
        pivot_values = [
            row.get(shape_plan.pivot_value_field) for row in ordered_rows
        ]
        pivot_values.extend([None] * (pivot_column_count - len(pivot_values)))
        shaped_row.update(dict(zip(dynamic_keys, pivot_values, strict=True)))
        shaped_rows.append(shaped_row)
    return ResultShapingSubgraphResult(
        status="success",
        columns=columns,
        rows=shaped_rows,
        source_row_count=len(rows),
        result_row_count=len(shaped_rows),
    )


class ResultShapingSubgraph:
    """兼容旧双计划协议的确定性塑形子图。"""

    # 构建单节点 LangGraph 子图，使塑形阶段保持独立并可由其他流水线复用。
    def __init__(self) -> None:
        workflow = StateGraph(_ResultShapingState)
        workflow.add_node("shape_result", self._shape_result)
        workflow.add_edge(START, "shape_result")
        workflow.add_edge("shape_result", END)
        self._workflow = workflow.compile()

    # 根据计划类型选择透传或转列实现，任何契约错误都返回失败而不输出部分结果。
    def _shape_result(self, state: _ResultShapingState) -> dict[str, Any]:
        query_plan = state["query_plan"]
        shape_plan = state["shape_plan"]
        rows = state["rows"]
        try:
            _validate_required_fields(query_plan, shape_plan)
            result = (
                _shape_passthrough_rows(query_plan, shape_plan, rows)
                if shape_plan.shape_type == "passthrough"
                else _shape_pivot_rows(query_plan, shape_plan, rows)
            )
        except (KeyError, TypeError, ValueError) as error:
            result = ResultShapingSubgraphResult(
                status="failure",
                source_row_count=len(rows),
                error=f"结果塑形失败：{error}",
            )
        return {"result": result}

    # 校验输入计划并运行塑形子图，调用方始终得到结构化成功或失败终态。
    def run(
        self,
        query_plan: NaturalLanguageQueryPlan,
        shape_plan: ResultShapePlan,
        rows: list[dict[str, Any]],
    ) -> ResultShapingSubgraphResult:
        state = self._workflow.invoke(
            {
                "query_plan": query_plan,
                "shape_plan": shape_plan,
                "rows": rows,
            }
        )
        return state["result"]


# 序列化塑形模型的原始响应，便于协议错误时在受限诊断日志中回放。
def _serialize_material_shaping_response(response: Any) -> str:
    model_dump_json = getattr(response, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json(indent=2)
    return repr(response)


# 构造无工具调用 ID 时的普通协议反馈，并在需要时重复全局工具标签语法。
def _build_material_shaping_protocol_feedback(
    code: str,
    message: str,
    repair_action: str,
    tool_tag_template: str | None,
) -> dict[str, str]:
    error: dict[str, Any] = {
        "code": code,
        "tool_name": SUBMIT_MATERIAL_SHAPE_PLAN_TOOL_NAME,
        "message": message,
        "repair_action": repair_action,
    }
    if tool_tag_template is not None:
        error["tool_call_format_guidance"] = {
            "instruction": "仅参考标签语法，并使用本轮真实工具名和 JSON 参数。",
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
                "next_action": repair_action,
            },
            ensure_ascii=False,
        ),
    }


# 将工具调用的语义错误作为原 tool_call_id 的正常失败结果返回，保留标准对话结构。
def _build_material_shaping_tool_feedback(
    tool_call_id: str,
    code: str,
    message: str,
    repair_action: str,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": SUBMIT_MATERIAL_SHAPE_PLAN_TOOL_NAME,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": code,
                    "message": message,
                    "repair_action": repair_action,
                },
                "retryable": True,
            },
            ensure_ascii=False,
        ),
    }


# 校验模型计划只能引用 SQL 实际输出列，并保证动态列键不会覆盖普通展示字段。
def _validate_material_shape_plan(
    shape_plan: MaterialResultShapePlan,
    result_columns: list[str],
    dynamic_column_contract: DynamicColumnContract,
) -> None:
    available_fields = set(result_columns)
    referenced_fields = {
        item.source_field for item in shape_plan.passthrough_columns
    }
    referenced_fields.update(shape_plan.group_fields)
    if shape_plan.pivot_value_field is not None:
        referenced_fields.add(shape_plan.pivot_value_field)
    if shape_plan.pivot_order_field is not None:
        referenced_fields.add(shape_plan.pivot_order_field)
    unknown_fields = sorted(referenced_fields - available_fields)
    if unknown_fields:
        raise MaterialShapePlanValidationError(
            "shaping_unknown_result_columns",
            "塑形计划引用了 SQL 结果中不存在的列：" + "、".join(unknown_fields),
            (
                "删除这些列引用，并只从 raw_material_headers 中选择准确列名："
                + "、".join(result_columns)
                + "。"
            ),
        )
    if not shape_plan.passthrough_columns and shape_plan.shape_type == "passthrough":
        raise MaterialShapePlanValidationError(
            "shaping_empty_passthrough_columns",
            "passthrough 没有任何可见字段。",
            "按照塑形指导，从 raw_material_headers 中选择至少一个字段写入 "
            "passthrough_columns。",
        )
    if (
        dynamic_column_contract.mode == "not_applicable"
        and shape_plan.shape_type != "passthrough"
    ):
        raise MaterialShapePlanValidationError(
            "shaping_unexpected_pivot",
            "塑形指导声明动态列数量不适用，但布局计划使用了 pivot。",
            "将 shape_type 改为 passthrough，并清空所有 pivot 专属字段。",
        )
    if (
        dynamic_column_contract.mode in {"auto", "fixed"}
        and shape_plan.shape_type != "pivot"
    ):
        raise MaterialShapePlanValidationError(
            "shaping_pivot_required",
            "塑形指导要求生成动态列，但布局计划没有使用 pivot。",
            "将 shape_type 改为 pivot，并按指导填写分组、排序和动态列字段。",
        )
    if (
        dynamic_column_contract.mode == "auto"
        and shape_plan.expected_pivot_columns is not None
    ):
        raise MaterialShapePlanValidationError(
            "shaping_auto_column_count_mismatch",
            "塑形指导要求由完整结果决定动态列数量，但计划填写了固定列数。",
            "将 expected_pivot_columns 改为 null，其他布局字段保持不变。",
        )
    if (
        dynamic_column_contract.mode == "fixed"
        and shape_plan.expected_pivot_columns != dynamic_column_contract.fixed_count
    ):
        raise MaterialShapePlanValidationError(
            "shaping_fixed_column_count_mismatch",
            "塑形计划的固定动态列数量与已确认指导不一致。",
            (
                "将 expected_pivot_columns 精确改为 "
                f"{dynamic_column_contract.fixed_count}，其他布局字段保持不变。"
            ),
        )
    if shape_plan.shape_type == "pivot":
        assert shape_plan.column_key_prefix is not None
        visible_keys = {item.output_key for item in shape_plan.passthrough_columns}
        if any(
            key == shape_plan.column_key_prefix
            or key.startswith(f"{shape_plan.column_key_prefix}_")
            for key in visible_keys
        ):
            raise MaterialShapePlanValidationError(
                "shaping_dynamic_key_conflict",
                "动态列键前缀与普通展示字段 output_key 冲突。",
                (
                    "修改 column_key_prefix，使其不等于且不作为任何 "
                    "passthrough_columns.output_key 的下划线前缀。"
                ),
            )


# 按模型已编译且通过校验的计划逐行透传原始值，不允许增加计算或筛选。
def _shape_material_passthrough_rows(
    shape_plan: MaterialResultShapePlan,
    rows: list[dict[str, Any]],
) -> ResultShapingSubgraphResult:
    columns = [
        ShapedResultColumn(
            key=item.output_key,
            label=item.label,
            source_result_field=item.source_field,
        )
        for item in shape_plan.passthrough_columns
    ]
    shaped_rows = [
        {
            item.output_key: row.get(item.source_field)
            for item in shape_plan.passthrough_columns
        }
        for row in rows
    ]
    return ResultShapingSubgraphResult(
        status="success",
        columns=columns,
        rows=shaped_rows,
        source_row_count=len(rows),
        result_row_count=len(shaped_rows),
        material_shape_plan=shape_plan,
    )


# 按稳定主体键聚合完整原料并横向展开成员值，拒绝同组普通字段出现矛盾。
def _shape_material_pivot_rows(
    shape_plan: MaterialResultShapePlan,
    rows: list[dict[str, Any]],
) -> ResultShapingSubgraphResult:
    assert shape_plan.pivot_value_field is not None
    assert shape_plan.column_key_prefix is not None
    assert shape_plan.column_label_pattern is not None
    grouped_rows: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        group_key = tuple(row.get(field) for field in shape_plan.group_fields)
        grouped_rows.setdefault(group_key, []).append(row)
    for group_rows in grouped_rows.values():
        for column in shape_plan.passthrough_columns:
            first_value = group_rows[0].get(column.source_field)
            if any(
                row.get(column.source_field) != first_value
                for row in group_rows[1:]
            ):
                raise ValueError(
                    f"同一塑形分组内的透传字段 {column.source_field} 存在多个不同值"
                )
    maximum_group_size = max(
        (len(group_rows) for group_rows in grouped_rows.values()),
        default=0,
    )
    pivot_column_count = shape_plan.expected_pivot_columns or maximum_group_size
    if maximum_group_size > pivot_column_count:
        raise ValueError("实际分组成员数量超过 expected_pivot_columns")
    dynamic_keys = [
        f"{shape_plan.column_key_prefix}_{index}"
        for index in range(1, pivot_column_count + 1)
    ]
    columns = [
        ShapedResultColumn(
            key=item.output_key,
            label=item.label,
            source_result_field=item.source_field,
        )
        for item in shape_plan.passthrough_columns
    ] + [
        ShapedResultColumn(
            key=dynamic_key,
            label=shape_plan.column_label_pattern.format(index=index),
            source_result_field=shape_plan.pivot_value_field,
        )
        for index, dynamic_key in enumerate(dynamic_keys, start=1)
    ]
    shaped_rows: list[dict[str, Any]] = []
    for group_rows in grouped_rows.values():
        ordered_rows = list(group_rows)
        if shape_plan.pivot_order_field is not None:
            ordered_rows.sort(
                key=lambda row: _build_sort_key(
                    row.get(shape_plan.pivot_order_field)
                )
            )
        shaped_row = {
            item.output_key: ordered_rows[0].get(item.source_field)
            for item in shape_plan.passthrough_columns
        }
        pivot_values = [
            row.get(shape_plan.pivot_value_field) for row in ordered_rows
        ]
        pivot_values.extend([None] * (pivot_column_count - len(pivot_values)))
        shaped_row.update(dict(zip(dynamic_keys, pivot_values, strict=True)))
        shaped_rows.append(shaped_row)
    return ResultShapingSubgraphResult(
        status="success",
        columns=columns,
        rows=shaped_rows,
        source_row_count=len(rows),
        result_row_count=len(shaped_rows),
        material_shape_plan=shape_plan,
    )


class MaterialResultShapingSubgraph:
    """在 SQL 执行后把自然语言塑形指导编译并确定性应用于完整原始结果。"""

    # 保存统一模型供应商参数，并装配“编译布局、执行布局”两个 LangGraph 节点。
    def __init__(
        self,
        client: Any,
        model: str,
        max_tokens: int = DEFAULT_SHAPING_MAX_TOKENS,
        request_profile: str = "deepseek",
        tool_tag_template: str | None = None,
        trace_writer: TraceWriter | None = None,
        message_trace_queue: ModelMessageTraceQueue | None = None,
        close_client_after_run: bool = False,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._request_profile = get_model_request_profile(request_profile)
        self._tool_tag_template = tool_tag_template
        self._trace_writer = trace_writer
        self._message_trace_queue = message_trace_queue
        self._close_client_after_run = close_client_after_run
        workflow = StateGraph(_MaterialResultShapingState)
        workflow.add_node("compile_shape_plan", self._compile_shape_plan)
        workflow.add_node("shape_rows", self._shape_rows)
        workflow.add_edge(START, "compile_shape_plan")
        workflow.add_edge("compile_shape_plan", "shape_rows")
        workflow.add_edge("shape_rows", END)
        self._workflow = workflow.compile()

    # 从全局供应商和工具标签配置创建异步塑形子图，保持整条查询链路协议一致。
    @classmethod
    def from_settings(
        cls,
        domain_profile: QueryDomainProfile,
        settings: Settings | None = None,
        trace_writer: TraceWriter | None = None,
        message_trace_queue: ModelMessageTraceQueue | None = None,
    ) -> "MaterialResultShapingSubgraph":
        del domain_profile
        resolved_settings = settings or get_settings()
        connection = resolve_model_provider_connection(resolved_settings)
        client = AsyncOpenAI(
            api_key=connection.api_key,
            base_url=connection.base_url,
            timeout=connection.timeout_seconds,
        )
        return cls(
            client=client,
            model=connection.model,
            max_tokens=resolved_settings.deepseek_query_shaping_max_tokens,
            request_profile=connection.provider,
            tool_tag_template=load_tool_tag_template(
                resolve_query_tool_tag_template_filename(resolved_settings)
            ),
            trace_writer=trace_writer,
            message_trace_queue=message_trace_queue,
            close_client_after_run=True,
        )

    # 仅向内部诊断出口写入塑形阶段轨迹，正式 HTTP 结果不暴露模型原始内容。
    def _write_trace(self, content: str) -> None:
        if self._trace_writer is not None:
            self._trace_writer(content)

    # 使用 auto Function Calling 编译塑形指导，并对协议、Schema 和字段引用有限修复。
    async def _compile_shape_plan(
        self,
        state: _MaterialResultShapingState,
    ) -> dict[str, Any]:
        try:
            dynamic_column_contract = parse_dynamic_column_contract(
                state["shaping_guidance"]
            )
        except DynamicColumnContractError as error:
            raise RuntimeError(f"原料塑形指导不符合动态列数量契约：{error}") from error
        messages: list[Any] = build_material_shaping_messages(
            state["shaping_guidance"],
            state["result_columns"],
            state["rows"][:MATERIAL_SHAPING_PREVIEW_ROW_COUNT],
            self._tool_tag_template,
        )
        tool = build_material_shape_plan_tool_definition()
        raw_responses: list[str] = []
        last_error = "塑形模型未提交有效计划。"
        for attempt in range(MAX_MATERIAL_SHAPING_REPAIR_COUNT + 1):
            response = await create_traced_chat_completion(
                client=self._client,
                message_queue=self._message_trace_queue,
                node="shaping",
                model=self._model,
                messages=[*messages],
                tools=[tool],
                tool_choice="auto",
                **self._request_profile.build_non_thinking_options(self._max_tokens),
            )
            raw_responses.append(_serialize_material_shaping_response(response))
            choice = response.choices[0]
            message = choice.message
            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                last_error = "塑形模型没有调用布局计划工具。"
                if attempt >= MAX_MATERIAL_SHAPING_REPAIR_COUNT:
                    break
                messages.extend(
                    (
                        message,
                        _build_material_shaping_protocol_feedback(
                            "shaping_tool_call_missing",
                            last_error,
                            "只调用一次 submit_material_shape_plan，不要输出普通文本。",
                            self._tool_tag_template,
                        ),
                    )
                )
                continue
            messages.append(message)
            if len(tool_calls) != 1:
                last_error = f"塑形模型本轮调用了 {len(tool_calls)} 个工具。"
                if attempt >= MAX_MATERIAL_SHAPING_REPAIR_COUNT:
                    break
                messages.extend(
                    _build_material_shaping_tool_feedback(
                        item.id,
                        "shaping_multiple_tool_calls",
                        last_error,
                        "重新生成，并且只调用一次 submit_material_shape_plan。",
                    )
                    for item in tool_calls
                )
                continue
            tool_call = tool_calls[0]
            if tool_call.function.name != SUBMIT_MATERIAL_SHAPE_PLAN_TOOL_NAME:
                last_error = f"塑形节点未注册工具 {tool_call.function.name}。"
                if attempt >= MAX_MATERIAL_SHAPING_REPAIR_COUNT:
                    break
                messages.append(
                    _build_material_shaping_tool_feedback(
                        tool_call.id,
                        "shaping_unexpected_tool",
                        last_error,
                        "只调用 submit_material_shape_plan。",
                    )
                )
                continue
            try:
                shape_plan = parse_material_shape_plan_tool_arguments(
                    tool_call.function.arguments
                )
                _validate_material_shape_plan(
                    shape_plan,
                    state["result_columns"],
                    dynamic_column_contract,
                )
            except ValidationError as error:
                last_error = "塑形工具参数未通过 Schema 校验。"
                schema_feedback = build_tool_argument_error_message(
                    tool_call.id,
                    SUBMIT_MATERIAL_SHAPE_PLAN_TOOL_NAME,
                    error,
                )
                if attempt >= MAX_MATERIAL_SHAPING_REPAIR_COUNT:
                    break
                messages.append(schema_feedback)
                continue
            except MaterialShapePlanValidationError as error:
                last_error = str(error)
                if attempt >= MAX_MATERIAL_SHAPING_REPAIR_COUNT:
                    break
                messages.append(
                    _build_material_shaping_tool_feedback(
                        tool_call.id,
                        error.code,
                        last_error,
                        error.repair_action,
                    )
                )
                continue
            return {
                "shape_plan": shape_plan,
                "raw_model_responses": raw_responses,
            }
        raise RuntimeError(last_error)

    # 对完整 SQL 原始结果确定性执行布局计划，失败时不返回部分塑形数据。
    def _shape_rows(self, state: _MaterialResultShapingState) -> dict[str, Any]:
        shape_plan = state["shape_plan"]
        rows = state["rows"]
        try:
            result = (
                _shape_material_passthrough_rows(shape_plan, rows)
                if shape_plan.shape_type == "passthrough"
                else _shape_material_pivot_rows(shape_plan, rows)
            )
            result = result.model_copy(
                update={"raw_model_responses": state["raw_model_responses"]}
            )
        except (KeyError, TypeError, ValueError) as error:
            result = ResultShapingSubgraphResult(
                status="failure",
                source_row_count=len(rows),
                material_shape_plan=shape_plan,
                raw_model_responses=state["raw_model_responses"],
                error=f"结果塑形失败：{error}",
            )
        return {"result": result}

    # 异步执行 SQL 后置塑形子图，并在结束后释放自行创建的模型客户端。
    async def run(
        self,
        shaping_guidance: str,
        result_columns: list[str],
        rows: list[dict[str, Any]],
    ) -> ResultShapingSubgraphResult:
        if not shaping_guidance.strip():
            raise ValueError("原料塑形指导不能为空")
        if not result_columns:
            raise ValueError("SQL 结果字段列表不能为空")
        try:
            try:
                final_state = await self._workflow.ainvoke(
                    {
                        "shaping_guidance": shaping_guidance.strip(),
                        "result_columns": result_columns,
                        "rows": rows,
                    }
                )
            except Exception as error:
                self._write_trace(f"[塑形层] 执行失败：{error}")
                return ResultShapingSubgraphResult(
                    status="failure",
                    source_row_count=len(rows),
                    error=str(error)[:500],
                )
            return final_state["result"]
        finally:
            if self._close_client_after_run:
                await self._client.close()
