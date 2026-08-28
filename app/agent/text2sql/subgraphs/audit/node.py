"""定义查询结果格式化、程序统计与相关性审计子图节点。"""

import json
import re
from collections.abc import Callable
from collections import Counter
from decimal import Decimal
from typing import Any, Final, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.text2sql.domains.base import QueryDomainProfile
from app.core.config import Settings, get_settings
from app.agent.text2sql.subgraphs.alignment.node import BusinessAlignmentResult
from app.agent.text2sql.subgraphs.planning.node import QueryPlanningAgentResult
from app.agent.text2sql.subgraphs.shaping.node import ResultShapingSubgraphResult
from app.agent.text2sql.shared.model_options import (
    DEFAULT_AUDIT_MAX_TOKENS,
    get_model_request_profile,
    resolve_model_provider_connection,
)
from app.agent.text2sql.shared.tool_tag_template import (
    load_tool_tag_template,
    resolve_query_tool_tag_template_filename,
)
from app.agent.text2sql.shared.yaml_context import render_yaml_context
from app.agent.text2sql.shared.tools.argument_feedback import (
    build_tool_argument_error_message,
)
from app.agent.text2sql.subgraphs.sql.node import SqlQuerySubgraphResult
from app.agent.text2sql.subgraphs.audit.tool import (
    SUBMIT_QUERY_RESULT_AUDIT_TOOL_NAME,
    build_query_result_audit_tool_definition,
    parse_query_result_audit_tool_arguments,
)


AUDIT_RESULT_PREVIEW_ROWS: Final[int] = 5
MAX_AUDIT_ARGUMENT_REPAIR_COUNT: Final[int] = 1
MAX_CATEGORY_DISTINCT_VALUES: Final[int] = 20
MAX_CATEGORY_VALUE_COUNTS: Final[int] = 10
IDENTIFIER_SUFFIXES: Final[tuple[str, ...]] = ("id", "_id", "uuid", "_uuid")
TEXT_DETAIL_TOKENS: Final[tuple[str, ...]] = (
    "url",
    "path",
    "image",
    "note",
    "comment",
    "content",
    "description",
)
TEMPORAL_TOKENS: Final[tuple[str, ...]] = ("date", "time", "_at")
CATEGORY_NAME_TOKENS: Final[tuple[str, ...]] = ("status", "type", "category", "level")
NumericValue = int | float | Decimal


class QueryResultHeader(BaseModel):
    """前端结果表的一列定义，键严格来自实际 SQL 返回列。"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="实际 SQL 返回列名")
    label: str = Field(description="供前端展示的人类可读列标题")


class QueryDisplayResult(BaseModel):
    """不经过模型改写的前端表格结果。"""

    model_config = ConfigDict(extra="forbid")

    headers: list[QueryResultHeader] = Field(description="按实际 SQL 列顺序排列的表头")
    rows: list[dict[str, Any]] = Field(description="按表头键筛选并 JSON 安全化后的完整结果行")
    returned_row_count: int = Field(description="SQL 实际返回的完整结果行数")
    effective_limit: int | None = Field(description="规划层设置的实际查询上限")
    limit_reached: bool = Field(description="实际返回行数是否达到规划上限")


class CategoryValueCount(BaseModel):
    """类别字段中一个取值及其由程序计算的出现次数。"""

    model_config = ConfigDict(extra="forbid")

    value: Any = Field(description="JSON 安全化后的类别值")
    count: int = Field(description="该值在完整结果中的出现次数")


class CategoryFieldStatistics(BaseModel):
    """适合按类别理解的字段分布统计。"""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(description="类别字段名")
    distinct_count: int = Field(description="完整结果中的不同非空值数量")
    null_count: int = Field(description="完整结果中的空值数量")
    value_counts: list[CategoryValueCount] = Field(description="按次数降序排列的类别分布")


class NumericFieldExtremes(BaseModel):
    """业务数值字段在完整结果中的最小值和最大值。"""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(description="业务数值字段名")
    non_null_count: int = Field(description="参与极值计算的非空数值数量")
    null_count: int = Field(description="完整结果中的空值数量")
    minimum: int | float | str = Field(description="完整结果中的最小值")
    maximum: int | float | str = Field(description="完整结果中的最大值")


class QueryResultStatistics(BaseModel):
    """程序根据完整 SQL 结果计算的受控统计事实。"""

    model_config = ConfigDict(extra="forbid")

    row_count: int = Field(description="完整 SQL 结果行数")
    planned_limit: int | None = Field(description="规划层设置的行数上限")
    limit_reached: bool = Field(description="实际行数是否达到规划上限")
    category_fields: list[CategoryFieldStatistics] = Field(description="类别字段分布")
    numeric_extremes: list[NumericFieldExtremes] = Field(description="业务数值字段极值")


class QueryResultAuditAssessment(BaseModel):
    """审计模型对结果表是否满足用户需求的受约束结论。"""

    model_config = ConfigDict(extra="forbid")

    matches_user_request: bool = Field(description="返回表是否能够回答用户问题")
    relevance_explanation: str = Field(
        min_length=1,
        description="说明表中哪些字段和结果粒度能够解决用户问题",
    )
    table_description: str = Field(
        min_length=1,
        description="简洁说明返回表每行含义及主要列组成",
    )
    result_summary: str = Field(
        min_length=1,
        max_length=500,
        description="只复述程序 statistics 中已有事实的简洁总结，禁止引用或统计样本行具体值",
    )
    issues: list[str] = Field(description="不能满足需求时列出问题；没有问题时为空数组")


class QueryResultAuditResult(BaseModel):
    """末端审计子图的模型判断和确定性展示结果。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "failure"] = Field(description="审计调用是否正常完成")
    assessment: QueryResultAuditAssessment | None = Field(default=None, description="成功时的审计结论")
    statistics: QueryResultStatistics = Field(description="程序根据完整结果生成的统计事实")
    display_result: QueryDisplayResult = Field(description="供前端直接渲染的完整表格结果")
    error: str | None = Field(default=None, description="审计模型失败时的安全错误摘要")
    raw_model_response: str | None = Field(default=None, description="审计模型原始响应，仅供受限诊断回放")


class _QueryResultAuditState(TypedDict, total=False):
    """末端子图在本地分析和模型审计节点间传递的最小状态。"""

    original_question: str
    alignment_result: BusinessAlignmentResult
    planning_result: QueryPlanningAgentResult
    sql_result: SqlQuerySubgraphResult
    shaping_result: ResultShapingSubgraphResult | None
    display_result: QueryDisplayResult
    statistics: QueryResultStatistics
    assessment: QueryResultAuditAssessment
    raw_model_response: str
    error: str


# 将 OpenAI 兼容响应完整保存，便于审计结论异常时回放模型行为。
def _serialize_raw_response(response: Any) -> str:
    model_dump_json = getattr(response, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json(indent=2)
    return repr(response)


# 将一次或两次审计响应合并为可回放文本，确保修复重试不会覆盖首次错误响应。
def _serialize_raw_responses(raw_responses: list[str]) -> str | None:
    if not raw_responses:
        return None
    if len(raw_responses) == 1:
        return raw_responses[0]
    serialized_items: list[Any] = []
    for raw_response in raw_responses:
        try:
            serialized_items.append(json.loads(raw_response))
        except (json.JSONDecodeError, TypeError):
            serialized_items.append(raw_response)
    return json.dumps(serialized_items, ensure_ascii=False, indent=2)


# 将数据库标量安全转换为 JSON 值，避免日期、Decimal 等对象阻断输出序列化。
def _to_json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


# 判断字段名是否表达标识符，避免对主键和外键做无意义的类别或极值统计。
def _is_identifier_field(field_name: str) -> bool:
    normalized_name = field_name.lower()
    return normalized_name in IDENTIFIER_SUFFIXES or normalized_name.endswith(
        ("_id", "_uuid")
    )


# 判断字段是否属于长文本、资源路径或时间信息，这些字段不适合作为类别分布。
def _is_non_categorical_field(field_name: str) -> bool:
    normalized_name = field_name.lower()
    return any(token in normalized_name for token in TEXT_DETAIL_TOKENS) or any(
        token in normalized_name for token in TEMPORAL_TOKENS
    )


# 判断非空列值是否全部为数值，布尔值不作为连续数值参与极值分析。
def _contains_only_numeric_values(values: list[Any]) -> bool:
    return bool(values) and all(
        isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
        for value in values
    )


# 判断字段名是否显式表达状态或类型，数值编码的类别字段不应参与极值分析。
def _is_named_category_field(field_name: str) -> bool:
    normalized_name = field_name.lower()
    return any(token in normalized_name for token in CATEGORY_NAME_TOKENS)


# 判断字段是否可作为业务数值度量；除标识符和显式类别编码外的数值列均可计算极值。
def _is_numeric_measure_field(field_name: str) -> bool:
    return not _is_identifier_field(field_name) and not _is_named_category_field(
        field_name
    )


# 识别凭证主键字段，防止为了图片调用而产生的内部用途提示混入表头。
def _is_proof_record_id_field(field_expression: str) -> bool:
    return re.search(
        r"\bproof_record\s*\.\s*id\b",
        field_expression,
        flags=re.IGNORECASE,
    ) is not None


# 根据查询计划的字段用途生成表头标签；无法确认映射时保留实际列名。
def _build_display_headers(
    result_columns: list[str],
    planning_result: QueryPlanningAgentResult,
) -> list[QueryResultHeader]:
    assert planning_result.query_plan is not None
    select_fields = planning_result.query_plan.select_fields
    headers: list[QueryResultHeader] = []
    for index, column_name in enumerate(result_columns):
        matched_purpose = next(
            (
                select_field.purpose
                for select_field in select_fields
                if select_field.result_field == column_name
                or select_field.field == column_name
                or select_field.field.rsplit(".", maxsplit=1)[-1] == column_name
            ),
            None,
        )
        if matched_purpose is None and index < len(select_fields):
            matched_purpose = select_fields[index].purpose
        matched_select_field = next(
            (
                select_field
                for select_field in select_fields
                if select_field.result_field == column_name
                or select_field.field == column_name
                or select_field.field.rsplit(".", maxsplit=1)[-1] == column_name
                or (
                    column_name.lower() == "proof_record_id"
                    and _is_proof_record_id_field(select_field.field)
                )
            ),
            None,
        )
        if matched_select_field is None and index < len(select_fields):
            candidate_select_field = select_fields[index]
            if _is_proof_record_id_field(candidate_select_field.field):
                matched_select_field = candidate_select_field
        if matched_select_field is not None and _is_proof_record_id_field(
            matched_select_field.field
        ):
            # 返回用途仅供内部联调与图片中转，表头只保留稳定的业务名称。
            matched_purpose = "凭证记录 ID"
        headers.append(
            QueryResultHeader(key=column_name, label=matched_purpose or column_name)
        )
    return headers


# 保持完整结果顺序并只保留声明列，确定性构造可供前端渲染的 JSON 表格。
def build_display_result(
    planning_result: QueryPlanningAgentResult,
    sql_result: SqlQuerySubgraphResult,
    shaping_result: ResultShapingSubgraphResult | None = None,
) -> QueryDisplayResult:
    if shaping_result is not None:
        if shaping_result.status != "success":
            raise ValueError("失败的塑形结果不能构造前端表格")
        headers = [
            QueryResultHeader(key=column.key, label=column.label)
            for column in shaping_result.columns
        ]
        rows = [
            {header.key: _to_json_safe(row.get(header.key)) for header in headers}
            for row in shaping_result.rows
        ]
        return QueryDisplayResult(
            headers=headers,
            rows=rows,
            returned_row_count=len(rows),
            effective_limit=sql_result.effective_limit,
            limit_reached=sql_result.limit_reached,
        )
    headers = _build_display_headers(sql_result.result_columns, planning_result)
    rows = [
        {header.key: _to_json_safe(row.get(header.key)) for header in headers}
        for row in sql_result.rows
    ]
    return QueryDisplayResult(
        headers=headers,
        rows=rows,
        returned_row_count=len(rows),
        effective_limit=sql_result.effective_limit,
        limit_reached=sql_result.limit_reached,
    )


# 对低基数字符串或布尔列计算完整结果分布，并排除标识符、时间和长文本字段。
def _build_category_statistics(
    result_columns: list[str],
    rows: list[dict[str, Any]],
) -> list[CategoryFieldStatistics]:
    category_statistics: list[CategoryFieldStatistics] = []
    for field_name in result_columns:
        if _is_identifier_field(field_name) or _is_non_categorical_field(field_name):
            continue
        values = [row.get(field_name) for row in rows]
        non_null_values = [value for value in values if value is not None]
        if not non_null_values:
            continue
        if _contains_only_numeric_values(non_null_values) and not _is_named_category_field(
            field_name
        ):
            continue
        safe_values = [_to_json_safe(value) for value in non_null_values]
        serialized_values = [
            json.dumps(value, ensure_ascii=False, sort_keys=True) for value in safe_values
        ]
        distinct_count = len(set(serialized_values))
        if distinct_count > MAX_CATEGORY_DISTINCT_VALUES:
            continue
        counts = Counter(serialized_values)
        first_value_by_key = dict(zip(serialized_values, safe_values, strict=True))
        value_counts = [
            CategoryValueCount(value=first_value_by_key[key], count=count)
            for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[
                :MAX_CATEGORY_VALUE_COUNTS
            ]
        ]
        category_statistics.append(
            CategoryFieldStatistics(
                field=field_name,
                distinct_count=distinct_count,
                null_count=len(values) - len(non_null_values),
                value_counts=value_counts,
            )
        )
    return category_statistics


# 对进度、积分和比例等业务数值字段计算完整结果极值，忽略标识符和非数值列。
def _build_numeric_extremes(
    result_columns: list[str],
    rows: list[dict[str, Any]],
) -> list[NumericFieldExtremes]:
    numeric_extremes: list[NumericFieldExtremes] = []
    for field_name in result_columns:
        if not _is_numeric_measure_field(field_name):
            continue
        values = [row.get(field_name) for row in rows]
        non_null_values = [value for value in values if value is not None]
        if not _contains_only_numeric_values(non_null_values):
            continue
        numeric_extremes.append(
            NumericFieldExtremes(
                field=field_name,
                non_null_count=len(non_null_values),
                null_count=len(values) - len(non_null_values),
                minimum=_to_json_safe(min(non_null_values)),
                maximum=_to_json_safe(max(non_null_values)),
            )
        )
    return numeric_extremes


# 基于完整 SQL 结果计算审计可依赖的行数、类别分布和业务数值极值。
def build_query_result_statistics(
    sql_result: SqlQuerySubgraphResult,
    shaping_result: ResultShapingSubgraphResult | None = None,
) -> QueryResultStatistics:
    result_columns = sql_result.result_columns
    rows = sql_result.rows
    if shaping_result is not None:
        if shaping_result.status != "success":
            raise ValueError("失败的塑形结果不能生成统计信息")
        result_columns = [column.key for column in shaping_result.columns]
        rows = shaping_result.rows
    return QueryResultStatistics(
        row_count=len(rows),
        planned_limit=sql_result.planned_limit,
        limit_reached=sql_result.limit_reached,
        category_fields=_build_category_statistics(
            result_columns,
            rows,
        ),
        numeric_extremes=_build_numeric_extremes(
            result_columns,
            rows,
        ),
    )


# 在没有工具调用 ID 时返回协议错误，并重复只含语法占位符的全局 tool-tag。
def _build_missing_audit_tool_feedback(
    error_code: str,
    message: str,
    repair_action: str,
    tool_tag_template: str | None,
) -> dict[str, str]:
    error: dict[str, Any] = {
        "code": error_code,
        "tool_name": SUBMIT_QUERY_RESULT_AUDIT_TOOL_NAME,
        "message": message,
        "repair_action": repair_action,
    }
    if tool_tag_template is not None:
        error["tool_call_format_guidance"] = {
            "instruction": (
                "template 只表示标签语法，禁止原样输出占位符；"
                "真实工具名和参数必须来自本轮 Function Calling Schema。"
            ),
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


# 将多工具或错误工具名作为原 tool_call_id 的正常失败结果返回模型。
def _build_audit_protocol_tool_error(
    tool_call_id: str,
    called_tool_name: str,
    error_code: str,
    message: str,
) -> dict[str, str]:
    repair_action = (
        f"重新生成本轮响应，并且只调用一次 {SUBMIT_QUERY_RESULT_AUDIT_TOOL_NAME}。"
    )
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": error_code,
                    "tool_name": called_tool_name,
                    "message": message,
                    "repair_action": repair_action,
                },
                "retryable": True,
                "next_action": repair_action,
            },
            ensure_ascii=False,
        ),
    }


# 将临时模型请求异常转换为不泄漏底层地址和凭证的可重试上下文。
def _build_audit_request_retry_message() -> dict[str, str]:
    return {
        "role": "user",
        "content": json.dumps(
            {
                "context_type": "model_request_feedback",
                "status": "failure",
                "error": {
                    "code": "audit_model_request_failed",
                    "message": "上一轮审计模型请求未完成，未产生可执行工具调用。",
                    "repair_action": (
                        "继续使用原有查询事实，并且只调用一次 "
                        f"{SUBMIT_QUERY_RESULT_AUDIT_TOOL_NAME}。"
                    ),
                },
                "retryable": True,
            },
            ensure_ascii=False,
        ),
    }


# 构造稳定审计提示词，限制模型只解释程序统计和受限样本与用户问题的对应关系。
def _build_audit_system_prompt(domain_display_name: str) -> str:
    return f"""你是{domain_display_name}查询的末端结果审计器。你需要判断返回表是否能够回答用户问题，并用简洁中文解释表结构、相关性和程序统计结果。

严格规则：
1. 必须且只能调用一次 submit_query_result_audit 工具提交结论，禁止输出普通文本或调用其他工具。
2. rows_preview 只用于理解一行含义和检查字段对应关系；其中状态、类型或布尔编码可能已经由程序按数据库字段注释转换为展示值。禁止反推原始编码，也禁止在 result_summary 中引用样本具体值、按样本计数或比较，或根据总行数猜测未展示行。
3. statistics 是程序基于完整结果计算的唯一统计事实。只能复述或解释其中的行数、类别分布和数值极值，不得自行重新统计或补充不存在的指标。
4. relevance_explanation 说明结果粒度、主要字段和筛选口径为何能或不能回答用户问题。
5. table_description 只说明一行代表什么及主要列分为哪些业务信息，不逐列复述字段，也不描述数据库实现。
6. result_summary 只能复述 statistics 已明确提供的行数、类别分布和数值极值；不得从 rows_preview 生成任何额外统计。没有可用类别或极值时只说明行数和结果范围。
7. 结论保持简洁，不提供操作建议，不生成 SQL，不重复用户原问题，不讨论模型或工作流阶段。

输入是合法 YAML，字段含义固定如下：
- `original_question` 是用户原始问题，`aligned_question` 是已完成业务对齐的查询需求。
- `query_plan` 是查询口径摘要；`query_goal` 是目标，`root_block_id` 是最终结果块。`query_blocks` 按依赖顺序列出每块的行粒度、筛选、量词、聚合后筛选和输出字段；非根块负责资格或聚合，根块决定最终每行含义。`business_caliber` 是业务口径。
- `executed_sql` 是已通过安全校验并实际执行的只读 SQL，只用于核对查询实现。
- `result_table.headers` 是表头列表；`key` 是实际结果字段，`label` 是中文展示名；`rows_preview` 只包含前 5 行样本，状态类值可能已经按字段注释翻译。
- `result_table.statistics.row_count` 是完整结果行数，`planned_limit` 是规划上限或 null，`limit_reached` 表示结果是否达到该上限。
- `result_table.statistics.category_fields` 是完整结果的类别统计；`field` 是字段，`distinct_count` 是非空不同值数量，`null_count` 是空值数，`value_counts` 中 `value` 与 `count` 是类别值及次数。
- `result_table.statistics.numeric_extremes` 是完整结果的数值极值；`field` 是字段，`non_null_count` 与 `null_count` 是非空和空值数，`minimum` 与 `maximum` 是最小值和最大值。

只能使用上述 YAML 字段的固定含义，样本行不能替代完整统计。"""


# 将问题、查询口径、最终 SQL、程序统计和受限样本组成审计模型的唯一动态上下文。
def _build_audit_messages(
    domain_display_name: str,
    original_question: str,
    alignment_result: BusinessAlignmentResult,
    planning_result: QueryPlanningAgentResult,
    sql_result: SqlQuerySubgraphResult,
    display_result: QueryDisplayResult,
    statistics: QueryResultStatistics,
    tool_tag_template: str | None = None,
) -> list[dict[str, str]]:
    assert alignment_result.aligned_request is not None
    assert planning_result.query_plan is not None
    context = {
        "original_question": original_question,
        "aligned_question": alignment_result.aligned_request.aligned_question,
        "query_plan": {
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
                    "select_fields": [
                        item.model_dump() for item in block.select_fields
                    ],
                }
                for block in planning_result.query_plan.query_blocks
            ],
            "business_caliber": [
                item.model_dump()
                for item in planning_result.query_plan.business_caliber
            ],
        },
        "result_shape_plan": (
            planning_result.result_shape_plan.model_dump()
            if planning_result.result_shape_plan is not None
            else None
        ),
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
            "content": _build_audit_system_prompt(domain_display_name),
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


class QueryResultAuditSubgraph:
    """先确定性格式化和统计，再让模型审计受限结果表的末端 LangGraph 子图。"""

    # 初始化异步审计模型和固定两节点工作流，模型只参与末端解释，不参与统计与格式化。
    def __init__(
        self,
        client: Any,
        model: str,
        domain_profile: QueryDomainProfile,
        max_tokens: int = DEFAULT_AUDIT_MAX_TOKENS,
        request_profile: str = "deepseek",
        tool_tag_template: str | None = None,
        trace_writer: Callable[[str], None] | None = None,
        close_client_after_run: bool = False,
    ) -> None:
        self._client = client
        self._model = model
        self._domain_profile = domain_profile
        self._max_tokens = max_tokens
        self._request_profile = get_model_request_profile(request_profile)
        self._tool_tag_template = tool_tag_template
        self._trace_writer = trace_writer
        self._close_client_after_run = close_client_after_run
        workflow = StateGraph(_QueryResultAuditState)
        workflow.add_node("analyze_result", self._analyze_result)
        workflow.add_node("audit_result", self._audit_result)
        workflow.add_edge(START, "analyze_result")
        workflow.add_edge("analyze_result", "audit_result")
        workflow.add_edge("audit_result", END)
        self._workflow = workflow.compile()

    # 从全局供应商配置和标准地址创建异步客户端，确保审计与前序查询阶段使用同一服务。
    @classmethod
    def from_settings(
        cls,
        domain_profile: QueryDomainProfile,
        settings: Settings | None = None,
        trace_writer: Callable[[str], None] | None = None,
    ) -> "QueryResultAuditSubgraph":
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
            domain_profile=domain_profile,
            max_tokens=resolved_settings.deepseek_query_audit_max_tokens,
            request_profile=connection.provider,
            tool_tag_template=load_tool_tag_template(
                resolve_query_tool_tag_template_filename(resolved_settings)
            ),
            trace_writer=trace_writer,
            close_client_after_run=True,
        )

    # 在显式启用内部追踪时记录审计调用和可修复错误，默认不输出模型原始内容。
    def _write_trace(self, content: str) -> None:
        if self._trace_writer is not None:
            self._trace_writer(content)

    # 使用完整结果确定性生成前端表格和统计事实，避免模型承担可计算工作。
    def _analyze_result(self, state: _QueryResultAuditState) -> dict[str, Any]:
        return {
            "display_result": build_display_result(
                state["planning_result"],
                state["sql_result"],
                state.get("shaping_result"),
            ),
            "statistics": build_query_result_statistics(
                state["sql_result"],
                state.get("shaping_result"),
            ),
        }

    # 使用 auto Function Calling 提交唯一审计结论，并分类反馈协议、参数及临时请求错误。
    async def _audit_result(self, state: _QueryResultAuditState) -> dict[str, Any]:
        messages: list[Any] = _build_audit_messages(
            self._domain_profile.display_name,
            state["original_question"],
            state["alignment_result"],
            state["planning_result"],
            state["sql_result"],
            state["display_result"],
            state["statistics"],
            self._tool_tag_template,
        )
        raw_responses: list[str] = []
        audit_tool = build_query_result_audit_tool_definition()
        last_error_message = "审计模型未提交有效结论。"
        api_call_failed = False

        for attempt in range(MAX_AUDIT_ARGUMENT_REPAIR_COUNT + 1):
            self._write_trace(
                f"[结果审计层] 第 {attempt + 1} 次模型调用：核对结果相关性和摘要"
            )
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[*messages],
                    tools=[audit_tool],
                    tool_choice="auto",
                    **self._request_profile.build_non_thinking_options(
                        self._max_tokens,
                    ),
                )
            except Exception:
                api_call_failed = True
                last_error_message = "审计模型请求未完成。"
                if attempt < MAX_AUDIT_ARGUMENT_REPAIR_COUNT:
                    feedback_message = _build_audit_request_retry_message()
                    messages.append(feedback_message)
                    self._write_trace(
                        "[结果审计层] 模型请求失败：" + feedback_message["content"]
                    )
                    continue
                break

            api_call_failed = False
            raw_responses.append(_serialize_raw_response(response))
            choice = response.choices[0]
            message = choice.message
            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                truncated = getattr(choice, "finish_reason", None) == "length"
                error_code = (
                    "audit_tool_call_truncated"
                    if truncated
                    else "audit_tool_call_missing"
                )
                last_error_message = (
                    "审计模型输出达到长度上限且没有形成工具调用。"
                    if truncated
                    else "审计模型没有调用结果审计工具。"
                )
                repair_action = (
                    "缩短说明字段，并且只调用一次 submit_query_result_audit。"
                    if truncated
                    else "只调用一次 submit_query_result_audit，不要输出普通文本。"
                )
                if attempt < MAX_AUDIT_ARGUMENT_REPAIR_COUNT:
                    feedback_message = _build_missing_audit_tool_feedback(
                        error_code,
                        last_error_message,
                        repair_action,
                        self._tool_tag_template,
                    )
                    messages.extend((message, feedback_message))
                    self._write_trace(
                        "[结果审计层] 工具协议校验失败："
                        + feedback_message["content"]
                    )
                    continue
                break

            messages.append(message)
            if len(tool_calls) != 1:
                last_error_message = (
                    "结果审计每轮必须且只能调用一个工具，"
                    f"本轮实际调用 {len(tool_calls)} 个。"
                )
                if attempt < MAX_AUDIT_ARGUMENT_REPAIR_COUNT:
                    feedback_messages = [
                        _build_audit_protocol_tool_error(
                            tool_call.id,
                            tool_call.function.name,
                            "audit_multiple_tool_calls",
                            last_error_message,
                        )
                        for tool_call in tool_calls
                    ]
                    messages.extend(feedback_messages)
                    self._write_trace(
                        "[结果审计层] 工具数量校验失败："
                        + feedback_messages[-1]["content"]
                    )
                    continue
                break

            tool_call = tool_calls[0]
            if tool_call.function.name != SUBMIT_QUERY_RESULT_AUDIT_TOOL_NAME:
                last_error_message = (
                    f"工具 {tool_call.function.name} 未在结果审计层注册，"
                    "本次调用未执行。"
                )
                if attempt < MAX_AUDIT_ARGUMENT_REPAIR_COUNT:
                    feedback_message = _build_audit_protocol_tool_error(
                        tool_call.id,
                        tool_call.function.name,
                        "audit_unexpected_tool",
                        last_error_message,
                    )
                    messages.append(feedback_message)
                    self._write_trace(
                        "[结果审计层] 工具名称校验失败："
                        + feedback_message["content"]
                    )
                    continue
                break

            try:
                assessment = parse_query_result_audit_tool_arguments(
                    tool_call.function.arguments
                )
            except ValidationError as error:
                last_error_message = "结果审计工具参数未通过 Schema 校验。"
                if attempt < MAX_AUDIT_ARGUMENT_REPAIR_COUNT:
                    feedback_message = build_tool_argument_error_message(
                        tool_call.id,
                        SUBMIT_QUERY_RESULT_AUDIT_TOOL_NAME,
                        error,
                    )
                    messages.append(feedback_message)
                    self._write_trace(
                        "[结果审计层] 工具参数校验失败："
                        + feedback_message["content"]
                    )
                    continue
                break

            if attempt > 0:
                self._write_trace(
                    "[结果审计层] 已完成修正；节点结束后丢弃独立错误上下文"
                )
            self._write_trace("[结果审计层] 已生成受约束的结果相关性说明和摘要")
            return {
                "assessment": assessment,
                "raw_model_response": _serialize_raw_responses(raw_responses),
            }

        raw_model_response = _serialize_raw_responses(raw_responses)
        if api_call_failed:
            return {
                "error": "查询结果审计模型连续调用失败，请稍后重试。",
                "raw_model_response": raw_model_response,
            }
        return {
            "error": f"查询结果审计失败：{last_error_message}",
            "raw_model_response": raw_model_response,
        }

    # 异步执行末端子图；审计失败仍保留程序表格与统计，并释放自行创建的模型客户端。
    async def run(
        self,
        original_question: str,
        alignment_result: BusinessAlignmentResult,
        planning_result: QueryPlanningAgentResult,
        sql_result: SqlQuerySubgraphResult,
        shaping_result: ResultShapingSubgraphResult | None = None,
    ) -> QueryResultAuditResult:
        if not original_question.strip():
            raise ValueError("用户原问题不能为空")
        if alignment_result.status != "success" or alignment_result.aligned_request is None:
            raise ValueError("只有成功的业务对齐结果可以进入末端审计")
        if planning_result.status != "success" or planning_result.query_plan is None:
            raise ValueError("只有成功的查询规划结果可以进入末端审计")
        if sql_result.status != "success":
            raise ValueError("只有成功执行的 SQL 结果可以进入末端审计")
        if shaping_result is not None and shaping_result.status != "success":
            raise ValueError("只有成功的塑形结果可以进入末端审计")
        try:
            state = await self._workflow.ainvoke(
                {
                    "original_question": original_question.strip(),
                    "alignment_result": alignment_result,
                    "planning_result": planning_result,
                    "sql_result": sql_result,
                    "shaping_result": shaping_result,
                }
            )
        finally:
            if self._close_client_after_run:
                await self._client.close()
        if "error" in state:
            return QueryResultAuditResult(
                status="failure",
                statistics=state["statistics"],
                display_result=state["display_result"],
                error=state["error"],
                raw_model_response=state.get("raw_model_response"),
            )
        return QueryResultAuditResult(
            status="success",
            assessment=state["assessment"],
            statistics=state["statistics"],
            display_result=state["display_result"],
            raw_model_response=state.get("raw_model_response"),
        )
