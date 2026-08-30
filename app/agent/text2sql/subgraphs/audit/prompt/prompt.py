"""构造结果审计智能体的固定提示词与动态消息上下文。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from app.agent.text2sql.shared.yaml_context import render_yaml_context

if TYPE_CHECKING:
    from app.agent.text2sql.subgraphs.alignment.node import BusinessAlignmentResult
    from app.agent.text2sql.subgraphs.planning.node import QueryPlanningAgentResult
    from app.agent.text2sql.subgraphs.sql.node import SqlQuerySubgraphResult


AUDIT_RESULT_PREVIEW_ROWS: Final[int] = 5


# 构造稳定审计提示词，限制模型只解释程序统计和受限样本与用户问题的对应关系。
def build_audit_system_prompt(domain_display_name: str) -> str:
    return f"""# 角色

你是{domain_display_name}查询的末端结果审计器。

你的唯一任务是判断返回表是否能够回答用户问题，并用简洁中文解释表结构、相关性和程序统计结果。

# 输出协议

1. 必须且只能调用一次 `submit_query_result_audit` 工具提交结论。
2. 禁止输出普通文本或调用其他工具。

# 事实边界

1. `statistics` 是程序基于完整结果计算的唯一统计事实。
2. 只能复述或解释其中的行数、类别分布和数值极值，不得自行重新统计或补充不存在的指标。
3. `rows_preview` 只用于理解一行含义和检查字段对应关系。
4. 样本中的状态、类型或布尔编码可能已经由程序按数据库字段注释转换为展示值，禁止反推原始编码。
5. 禁止在 `result_summary` 中引用样本具体值、按样本计数或比较，也不得根据总行数猜测未展示行。

# 字段要求

- `relevance_explanation`：说明结果粒度、主要字段和筛选口径为何能或不能回答用户问题。
- `table_description`：只说明一行代表什么及主要列分为哪些业务信息，不逐列复述字段，也不描述数据库实现。
- `result_summary`：只能复述 `statistics` 已明确提供的行数、类别分布和数值极值；没有可用类别或极值时，只说明行数和结果范围。
- `issues`：只有结果不能满足需求时才列出具体问题；没有问题时为空数组。

# 表达要求

- 结论保持简洁。
- 不提供操作建议。
- 不生成 SQL。
- 不重复用户原问题。
- 不讨论模型或工作流阶段。

# 输入字段

- `original_question`：用户原始问题。
- `aligned_question`：已经完成业务对齐的查询需求。
- `query_plan`：查询口径摘要；新原料协议提供 `guidance` 与 `required_tables`，历史协议提供查询目标、查询块和业务口径。
- `shaping_guidance`：已经确认的结果布局要求，只用于理解最终行列含义。
- `executed_sql`：已经通过安全校验并实际执行的只读 SQL，只用于核对查询实现。
- `result_table.headers`：表头列表；`key` 是实际结果字段，`label` 是中文展示名。
- `result_table.rows_preview`：最多前 5 行样本，只用于理解字段与行粒度。
- `result_table.statistics.row_count`：完整结果行数。
- `result_table.statistics.planned_limit`：规划上限或 `null`。
- `result_table.statistics.limit_reached`：结果是否达到规划上限。
- `result_table.statistics.category_fields`：完整结果的类别统计；`field` 是字段，`distinct_count` 是非空不同值数量，`null_count` 是空值数，`value_counts` 中的 `value` 与 `count` 是类别值及次数。
- `result_table.statistics.numeric_extremes`：完整结果的数值极值；`field` 是字段，`non_null_count` 与 `null_count` 是非空和空值数，`minimum` 与 `maximum` 是最小值和最大值。

只能使用上述 YAML 字段的固定含义，样本行不能替代完整统计。
"""


# 将新原料协议或兼容历史协议整理为审计模型使用的查询口径摘要。
def _build_query_plan_context(
    planning_result: QueryPlanningAgentResult,
) -> tuple[dict[str, Any], Any]:
    if planning_result.material_plan is not None:
        return (
            {
                "guidance": planning_result.material_plan.guidance,
                "required_tables": planning_result.material_plan.required_tables,
            },
            planning_result.material_plan.raw_material_shaping_guidance,
        )

    assert planning_result.query_plan is not None
    query_plan_context = {
        "query_goal": planning_result.query_plan.query_goal,
        "root_block_id": planning_result.query_plan.root_block_id,
        "query_blocks": [
            {
                "block_id": block.block_id,
                "role": block.role,
                "row_granularity": block.row_granularity,
                "filters": [item.model_dump() for item in block.filters],
                "quantified_conditions": [
                    item.model_dump() for item in block.quantified_conditions
                ],
                "having": [item.model_dump() for item in block.having],
                "select_fields": [item.model_dump() for item in block.select_fields],
            }
            for block in planning_result.query_plan.query_blocks
        ],
        "business_caliber": [
            item.model_dump() for item in planning_result.query_plan.business_caliber
        ],
    }
    shaping_guidance = (
        planning_result.result_shape_plan.model_dump()
        if planning_result.result_shape_plan is not None
        else None
    )
    return query_plan_context, shaping_guidance


# 将问题、查询口径、最终 SQL、程序统计和受限样本组成审计模型的唯一动态上下文。
def build_audit_messages(
    domain_display_name: str,
    original_question: str,
    alignment_result: BusinessAlignmentResult,
    planning_result: QueryPlanningAgentResult,
    sql_result: SqlQuerySubgraphResult,
    display_result: Any,
    statistics: Any,
    tool_tag_template: str | None = None,
) -> list[dict[str, str]]:
    assert alignment_result.aligned_request is not None
    query_plan_context, shaping_guidance = _build_query_plan_context(planning_result)
    context = {
        "original_question": original_question,
        "aligned_question": alignment_result.aligned_request.aligned_question,
        "query_plan": query_plan_context,
        "shaping_guidance": shaping_guidance,
        "executed_sql": sql_result.sql,
        "result_table": {
            "headers": [header.model_dump() for header in display_result.headers],
            "statistics": statistics.model_dump(),
            "rows_preview": display_result.rows[:AUDIT_RESULT_PREVIEW_ROWS],
        },
    }
    messages = [
        {
            "role": "system",
            "content": build_audit_system_prompt(domain_display_name),
        }
    ]
    if tool_tag_template is not None:
        messages.append(
            {
                "role": "user",
                "content": (
                    "# 工具调用标签语法\n\n"
                    "以下模板只说明服务端要求的标签语法，不定义任何具体工具或"
                    "业务参数；必须根据本轮 Function Calling Schema 替换全部占位符，"
                    "严禁原样输出占位符。\n\n"
                    f"{tool_tag_template}"
                ),
            }
        )
    messages.append(
        {
            "role": "user",
            "content": render_yaml_context(context),
        }
    )
    return messages


__all__ = [
    "AUDIT_RESULT_PREVIEW_ROWS",
    "build_audit_messages",
    "build_audit_system_prompt",
]
