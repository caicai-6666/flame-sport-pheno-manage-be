"""按规划层的独立塑形计划确定性整理已翻译的 SQL 结果。"""

from collections import OrderedDict
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from app.agent.tools.query_plan import NaturalLanguageQueryPlan, ResultShapePlan


class ShapedResultColumn(BaseModel):
    """塑形后一个稳定机器列名及其前端展示标题。"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="塑形后行对象使用的稳定列键")
    label: str = Field(description="供前端展示的人类可读列标题")


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


class _ResultShapingState(TypedDict, total=False):
    """塑形子图在输入与确定性执行节点之间传递的状态。"""

    query_plan: NaturalLanguageQueryPlan
    shape_plan: ResultShapePlan
    rows: list[dict[str, Any]]
    result: ResultShapingSubgraphResult


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
        ShapedResultColumn(key=field_name, label=source_labels[field_name])
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
        ShapedResultColumn(key=field_name, label=source_labels[field_name])
        for field_name in shape_plan.passthrough_fields
    ] + [
        ShapedResultColumn(
            key=dynamic_key,
            label=shape_plan.column_label_pattern.format(index=index),
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
    """在翻译后、统计审计前执行不调用模型的确定性塑形子图。"""

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
