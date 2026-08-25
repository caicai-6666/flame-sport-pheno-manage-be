"""使用独立 LangGraph 子图生成、校验并执行受限的最终只读 SQL。"""

import asyncio
import json
import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, TypedDict

import asyncmy
from asyncmy.cursors import DictCursor
from langgraph.graph import END, START, StateGraph
from openai import OpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlglot import exp, parse, parse_one
from sqlglot.errors import ParseError

from app.agent.domains.base import QueryDomainProfile
from app.agent.events.models import AgentProgressUpdate
from app.agent.events.publisher import AgentProgressReporter, ProgressEmitter
from app.core.config import Settings, get_settings
from app.agent.runtime.model_options import (
    DEFAULT_SQL_MAX_TOKENS,
    build_non_thinking_completion_options,
    build_strict_tools_base_url,
)
from app.agent.tools.query_plan import NaturalLanguageQueryPlan
from app.agent.tools.strict_schema import (
    StrictToolSchemaValidationError,
    build_strict_tool_definition,
)
from app.agent.runtime.table_schema_cache import CachingTableSchemaReader
from app.agent.runtime.table_schema_reader import InformationSchemaTableSchemaReader
from app.agent.runtime.yaml_context import parse_yaml_context, render_yaml_context
from app.agent.tools.table_schema import TableSchemaToolResponse
from app.agent.tools.argument_feedback import build_tool_argument_error_message


QUERY_EXECUTION_TIMEOUT_SECONDS: Final[float] = 10.0
DEFAULT_SQL_GENERATION_COUNT: Final[int] = 2
SUBMIT_SQL_QUERY_TOOL_NAME: Final[str] = "submit_sql_query"
TraceWriter = Callable[[str], None]
SchemaReader = Callable[[str], TableSchemaToolResponse]
SqlScalar = str | int | float | bool | None
SqlExecutor = Callable[[str, tuple[SqlScalar, ...]], list[dict[str, Any]]]
FORBIDDEN_FUNCTION_NAMES: Final[frozenset[str]] = frozenset(
    {
        "BENCHMARK",
        "CONNECTION_ID",
        "DATABASE",
        "GET_LOCK",
        "LAST_INSERT_ID",
        "LOAD_FILE",
        "RELEASE_LOCK",
        "SLEEP",
        "SYSTEM_USER",
        "USER",
        "UUID_SHORT",
    }
)


class SqlQueryParameter(BaseModel):
    """strict 工具中一个固定结构的 SQL 命名参数。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="不含冒号的命名占位符名称，例如 user_id",
    )
    value: SqlScalar = Field(description="该命名占位符对应的 JSON 标量值")


class SqlQueryDraft(BaseModel):
    """SQL 生成模型一次性返回的待校验查询草稿。"""

    model_config = ConfigDict(extra="forbid")

    sql: str = Field(description="MySQL 只读查询，仅允许一条 SELECT 或 WITH ... SELECT")
    parameters: list[SqlQueryParameter] = Field(
        default_factory=list,
        description="SQL 中命名占位符及其值组成的列表；无参数时传空列表",
    )
    result_columns: list[str] = Field(
        min_length=1,
        description="结果集返回列名或别名，按 SQL SELECT 顺序排列",
    )

    # 兼容内部调用直接传入的参数字典，但远端 strict Schema 始终只暴露封闭参数列表。
    @field_validator("parameters", mode="before")
    @classmethod
    def normalize_legacy_parameter_mapping(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return [
                {"name": parameter_name, "value": parameter_value}
                for parameter_name, parameter_value in value.items()
            ]
        return value

    # 拒绝重复参数名，避免列表转换为执行字典时发生静默覆盖。
    @model_validator(mode="after")
    def validate_unique_parameter_names(self) -> "SqlQueryDraft":
        parameter_names = [parameter.name for parameter in self.parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("parameters 不能包含重复的命名参数")
        return self


class SqlValidationError(ValueError):
    """携带稳定错误代码和重试归属的 SQL 静态校验异常。"""

    # 保存面向模型的修复动作和重试归属，避免上层依赖中文错误文本判断流程。
    def __init__(
        self,
        code: str,
        message: str,
        repair_action: str,
        retry_target: Literal[
            "sql_generation", "query_planning", "none"
        ] = "sql_generation",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.repair_action = repair_action
        self.retry_target = retry_target
        self.details = details or {}


class SqlExecutionError(RuntimeError):
    """携带数据库安全诊断和有限重试策略的只读 SQL 执行异常。"""

    # 保存稳定错误代码和明确修复动作，避免模型根据模糊“执行失败”盲目重写查询。
    def __init__(
        self,
        code: str,
        message: str,
        repair_action: str,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.repair_action = repair_action
        self.retryable = retryable
        self.details = details or {}


class SqlFailureDiagnostic(BaseModel):
    """内部可安全保留的 SQL 外部调用诊断信息。"""

    model_config = ConfigDict(extra="forbid")

    exception_type: str = Field(description="异常类型名称")
    status_code: int | None = Field(default=None, description="上游 HTTP 状态码")
    provider_code: str | None = Field(default=None, description="上游错误代码")
    message: str = Field(description="经过脱敏和长度限制的诊断消息")


class SqlQuerySubgraphResult(BaseModel):
    """最终 SQL 子图的可审计执行结果。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "failure"] = Field(description="子图执行状态")
    schema_results: list[TableSchemaToolResponse] = Field(
        default_factory=list,
        description="本次根据查询计划按需读取的表结构",
    )
    draft: SqlQueryDraft | None = Field(default=None, description="模型生成的原始 SQL 草稿")
    sql: str | None = Field(
        default=None,
        description="通过静态校验、面向调用方返回范围的参数化 SQL",
    )
    analysis_sql: str | None = Field(
        default=None,
        description="通过静态校验且保留命名占位符的 SQL，仅供后续 AST 来源分析",
    )
    result_columns: list[str] = Field(default_factory=list, description="查询结果列名")
    rows: list[dict[str, Any]] = Field(default_factory=list, description="只读查询实际返回的完整结果行")
    planned_limit: int | None = Field(
        default=None,
        description="查询规划层确定的返回行数；null 表示完整执行当前筛选结果",
    )
    effective_limit: int | None = Field(
        default=None,
        description="本次按规划上限实际执行的最大返回行数；未设置时为 null",
    )
    returned_row_count: int = Field(
        default=0,
        description="本次 SQL 实际返回的完整结果行数",
    )
    limit_reached: bool = Field(
        default=False,
        description="实际返回行数是否恰好达到规划上限；达到时结果可能仍被上限截断",
    )
    error: str | None = Field(default=None, description="失败时可安全展示的原因")
    error_code: str | None = Field(default=None, description="失败原因的稳定机器可读代码")
    error_diagnostic: SqlFailureDiagnostic | None = Field(
        default=None,
        description="模型或数据库外部调用失败时的安全诊断信息",
    )
    retry_target: Literal["sql_generation", "query_planning", "none"] = Field(
        default="none",
        description="失败应由 SQL 生成、查询规划或外部系统中的哪一层处理",
    )
    generation_count: int = Field(default=0, description="SQL 模型本次实际生成次数")
    max_generation_count: int = Field(
        default=DEFAULT_SQL_GENERATION_COUNT,
        description="SQL 模型允许的最大生成次数",
    )
    raw_model_response: str | None = Field(
        default=None,
        description="SQL 生成模型的原始响应，仅供受限诊断回放",
    )


class _SqlQueryState(TypedDict, total=False):
    """独立子图在生成、校验和执行节点间传递的最小状态。"""

    query_plan: NaturalLanguageQueryPlan
    provided_schema_results: list[TableSchemaToolResponse] | None
    schema_results: list[TableSchemaToolResponse]
    messages: list[Any]
    draft: SqlQueryDraft
    raw_model_responses: list[str]
    generation_count: int
    max_generation_count: int
    current_tool_call_id: str
    validated_sql: str
    analysis_sql: str
    parameter_values: tuple[SqlScalar, ...]
    effective_limit: int | None
    error: str
    error_code: str
    error_diagnostic: SqlFailureDiagnostic
    retry_target: Literal["sql_generation", "query_planning", "none"]
    next_action: Literal["generate_sql", "validate_sql", "execute_sql", "end"]
    result: SqlQuerySubgraphResult


# 将 OpenAI 兼容响应保留为内部诊断输出，便于定位生成模型不符合约束的原因。
def _serialize_raw_response(response: Any) -> str:
    model_dump_json = getattr(response, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json(indent=2)
    return repr(response)


# 合并多次 SQL 生成响应以保留首次错误和修复过程，单次响应继续保持原始格式。
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


# 提取外部模型异常的状态码、供应商代码和脱敏消息，禁止保留密钥或完整请求对象。
def _build_safe_failure_diagnostic(error: Exception) -> SqlFailureDiagnostic:
    status_code = getattr(error, "status_code", None)
    if not isinstance(status_code, int):
        status_code = None

    provider_code = getattr(error, "code", None)
    error_body = getattr(error, "body", None)
    if isinstance(error_body, dict):
        nested_error = error_body.get("error")
        if isinstance(nested_error, dict):
            provider_code = provider_code or nested_error.get("code")
    provider_code_text = str(provider_code)[:120] if provider_code is not None else None

    raw_message = str(error)
    redacted_message = re.sub(
        r"(?i)\b(sk-[A-Za-z0-9_-]+|bearer\s+[A-Za-z0-9._-]+)\b",
        "[REDACTED]",
        raw_message,
    )
    return SqlFailureDiagnostic(
        exception_type=type(error).__name__,
        status_code=status_code,
        provider_code=provider_code_text,
        message=redacted_message[:800],
    )


# 由 Pydantic 草稿模型生成 DeepSeek strict 工具定义，服务端和本地共同约束 SQL 提交结构。
def build_sql_query_tool_definition() -> dict[str, object]:
    return build_strict_tool_definition(
        SUBMIT_SQL_QUERY_TOOL_NAME,
        (
            "提交一条参数化 MySQL 只读查询草稿。外部筛选值必须使用命名占位符，"
            "包括状态值、进度阈值和 NOT EXISTS 内的成员条件常量；"
            "只有 EXISTS 投影的 SELECT 1 和 LIMIT 整数可以保留字面量。"
            "parameters 使用 name/value 固定结构列表，result_columns 必须与 SELECT 输出名称和顺序一致。"
        ),
        SqlQueryDraft,
    )


# 将静态校验异常转换为与正常函数返回一致的工具结果，供模型按明确责任和动作修复。
def build_sql_validation_error_result(error: SqlValidationError) -> dict[str, Any]:
    retryable = error.retry_target == "sql_generation"
    next_action = {
        "sql_generation": "修正 SQL 草稿后，重新调用 submit_sql_query。",
        "query_planning": "返回查询规划阶段修正结构化查询计划。",
        "none": "停止模型重试，由系统或调用方处理该错误。",
    }[error.retry_target]
    return {
        "status": "failure",
        "error": {
            "code": error.code,
            "tool_name": SUBMIT_SQL_QUERY_TOOL_NAME,
            "message": str(error),
            "details": error.details,
            "repair_action": error.repair_action,
        },
        "retryable": retryable,
        "retry_target": error.retry_target,
        "next_action": next_action,
    }


# 使用原 SQL 工具调用 ID 返回分类校验错误，让修复轮次保持合法的函数调用上下文。
def build_sql_validation_error_message(
    tool_call_id: str,
    error: SqlValidationError,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            build_sql_validation_error_result(error),
            ensure_ascii=False,
        ),
    }


# 将数据库执行错误转换为同一 SQL 工具调用的正常失败结果，模型只获得脱敏诊断和明确动作。
def build_sql_execution_error_message(
    tool_call_id: str,
    error: SqlExecutionError,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": error.code,
                    "tool_name": SUBMIT_SQL_QUERY_TOOL_NAME,
                    "message": str(error),
                    "details": error.details,
                    "repair_action": error.repair_action,
                },
                "retryable": error.retryable,
                "retry_target": "sql_generation" if error.retryable else "none",
                "next_action": (
                    "按 repair_action 修正 SQL 后重新调用 submit_sql_query。"
                    if error.retryable
                    else "停止模型重试，由系统处理数据库基础设施错误。"
                ),
            },
            ensure_ascii=False,
        ),
    }


# 为 SQL 生成模型构造稳定系统提示词，约束其只将已确认的查询计划翻译为 SQL。
def _build_sql_generation_system_prompt() -> str:
    """构造不擅自压缩完整查询结果的稳定 SQL 生成约束。"""
    return """你是 MySQL 只读 SQL 生成器。你只负责把调用方提供的结构化查询计划转换为一条可执行 SQL，不能补充、推测或更改业务口径。

严格规则：
1. 必须且只能调用一次 submit_sql_query 工具提交结果，禁止输出普通文本、Markdown、解释和代码围栏。
2. SQL 只能是一条 SELECT，或一条以 WITH 开头且最终为 SELECT 的查询；可按查询计划使用 JOIN、子查询或 CTE。
3. 只能读取“允许读取的表”中列出的真实表；不得 SELECT *，不得使用注释、分号、多语句、写操作、锁、文件操作、管理语句或高风险函数。
4. WHERE、HAVING、JOIN ON 以及 EXISTS/NOT EXISTS 子查询中的所有字符串、数值、布尔和日期筛选值都必须使用 :parameter_name 命名占位符，并在 parameters 列表中提供 {"name": "parameter_name", "value": 对应JSON标量}；这包括查询计划已经给出的状态值和进度阈值。只有 EXISTS/NOT EXISTS 投影位置的 SELECT 1 以及 LIMIT/OFFSET 整数可以保留字面量。无参数时传空列表，不得拼接用户文本或未确认值。LIMIT 必须使用整数常量，不能参数化。
5. 必须使用输入中提供的真实字段、外键和备注；基础表字段优先使用表名或 aliases 中声明的别名限定。SQL 必须完整实现查询计划中的目标、关联、筛选、集合量化条件、分组、聚合、HAVING、排序和分页。
6. 查询计划是唯一业务事实来源。不要读取或假定用户原问题、规划阶段的思考过程、表概述或任何未提供数据。
7. 每个 select_fields 项都带有稳定 `result_field`。SELECT 必须按 select_fields 顺序为每项显式使用 `AS result_field`，result_columns 必须按相同顺序精确填写这些 result_field，不得新增、删除、改名或重复。
8. 严格遵循查询计划的 pagination：limit 为 null 时不得自行添加 LIMIT；limit 为整数时必须使用该整数。不要为了审计模型上下文、性能猜测或其他自行判断增加或缩小结果范围。
9. 对 all 和 none 量词，成员 predicate 不能先作为外层普通 WHERE 删除反例。必须按 quantified_conditions 的实现方式完成主体资格判断；EXISTS、NOT EXISTS 或相关子查询必须在同一个子查询内完整实现 correlation_condition、全部 collection_filters 和正确方向的成员 predicate/反条件。若最终还要返回集合成员行，应先确定合格主体，再在外层关联并返回成员。
10. query_blocks 是不可跨越的条件作用域。每个非根查询块必须实现为与 block_id 同名的一个 CTE，且只能读取自身 source_tables 和 input_blocks；根查询块是最外层 SELECT。不得遗漏、增加或重命名计划查询块，也不得把某块的 JOIN、WHERE、GROUP BY、HAVING 或量词条件移动到其他块。
11. 每个查询块的 SELECT 必须按该块 select_fields 顺序显式使用 AS result_field。后续块只能通过 `input_block.result_field` 引用前置块输出。最终 result_columns 只填写根查询块的 result_field。
12. aliases 对查询块内的外层 SELECT 和嵌套子查询共同生效。必须精确使用计划已声明的角色别名，不得为 EXISTS/NOT EXISTS 成员表临时发明 `alias2`、`member_alias` 等新别名，也不得改回真实表名；量词成员表只在相关子查询使用时，不要把它额外加入外层 FROM 或 JOIN。

输入是合法 YAML，字段含义固定如下：
- `query_plan` 是 SQL 层唯一权威的数据获取计划；`query_goal` 是查询目标，`root_block_id` 标识最外层结果块，`query_blocks` 按依赖拓扑顺序排列。
- `tables` 列出参与表；每项的 `table_name` 是真实表名，`role` 是本次职责。
- 每个查询块的 `row_granularity` 和 `grain_fields` 定义本块行粒度；`source_tables` 是本块直接读取的真实表，`input_blocks` 是本块直接读取的前置 CTE。`aliases`、`joins`、`filters`、`quantified_conditions`、`group_by`、`aggregations`、`having` 和 `order_by` 只在所属块生效。
- `quantified_conditions.subject_key` 是资格主体键，数量型量词的 `member_key` 是去重计数键；`predicate`、`correlation_condition`、`collection_filters` 不能互相替代。
- `implemented_business_rules` 只把核心规则映射到 joins、filters、quantified_conditions 等已有计划组件，便于审计覆盖情况；不得把它当成第二份 SQL 表达式来源。SQL 只需准确实现被引用的正式计划组件。
- `select_fields` 是所属查询块的输出字段；`field` 是字段或表达式，`result_field` 是该块 SQL 必须输出的 AS 别名，`purpose` 是简短表头标签。
- `pagination.limit` 是根查询块最大结果行数或 null，`pagination.offset` 是根查询块偏移量。
- `business_caliber` 是必须落实的业务口径；`assumptions` 是已确认允许保留的假设。
- `allowed_table_schemas` 是允许读取的表结构列表；每项的 `table` 是真实表名，`columns` 是字段列表，`field_name`、`data_type`、`foreign_key`、`comment` 分别表示真实字段、数据库类型、外键目标和字段注释。

只能按以上固定含义读取 YAML，不能把 YAML 键名误当成数据库标识符。"""


# 将已确认的查询计划和按需读取的结构作为 SQL 生成模型的唯一动态上下文。
def _build_sql_generation_messages(
    query_plan: NaturalLanguageQueryPlan,
    schema_results: list[TableSchemaToolResponse],
) -> list[dict[str, str]]:
    context = {
        "query_plan": query_plan.model_dump(),
        "allowed_table_schemas": [
            parse_yaml_context(item.result) for item in schema_results
        ],
    }
    return [
        {
            "role": "system",
            "content": _build_sql_generation_system_prompt(),
        },
        {
            "role": "user",
            "content": render_yaml_context(context),
        },
    ]


# 校验计划中的表名属于当前业务域白名单，并在保持计划顺序的同时去重。
def _get_plan_table_names(
    query_plan: NaturalLanguageQueryPlan,
    allowed_table_names: frozenset[str],
) -> list[str]:
    table_names: list[str] = []
    seen_table_names: set[str] = set()
    for table in query_plan.tables:
        if table.table_name not in allowed_table_names:
            raise SqlValidationError(
                code="query_plan_table_forbidden",
                message=f"查询计划包含不允许读取的表：{table.table_name}",
                repair_action=(
                    f"从查询计划中删除表 `{table.table_name}` 及所有引用该表的"
                    "关联、筛选、返回、分组、聚合和排序表达式。"
                ),
                retry_target="query_planning",
                details={"table_name": table.table_name},
            )
        if table.table_name not in seen_table_names:
            seen_table_names.add(table.table_name)
            table_names.append(table.table_name)
    return table_names


# 拒绝 SQL 注释和常见多语句分隔符，避免解析器忽略尾部的危险语句或注释内容。
def _reject_comments_and_statement_separators(sql: str) -> None:
    if any(marker in sql for marker in ("--", "/*", "*/", "#", ";")):
        raise SqlValidationError(
            code="comments_or_separators_forbidden",
            message="SQL 不允许包含注释、分号或多语句分隔符",
            repair_action="删除全部注释和分号，只提交一条完整的只读查询。",
        )


# 从 SQLGlot AST 读取单个非负整数 LIMIT 或 OFFSET，拒绝表达式和参数化行数。
def _get_integer_clause_value(
    clause: exp.Expression | None,
    clause_name: str,
    expected_value: int,
) -> int:
    if clause is None:
        return 0
    expression = clause.args.get("expression")
    if not isinstance(expression, exp.Literal) or expression.is_string:
        raise SqlValidationError(
            code="invalid_pagination_literal",
            message=(
                f"SQL 的 {clause_name} 必须是查询计划指定的整数常量 "
                f"{expected_value}"
            ),
            repair_action=f"将 {clause_name} 精确改为整数常量 {expected_value}。",
            details={"clause": clause_name, "expected_value": expected_value},
        )
    try:
        value = int(expression.this)
    except (TypeError, ValueError) as error:
        raise SqlValidationError(
            code="invalid_pagination_literal",
            message=(
                f"SQL 的 {clause_name} 必须是查询计划指定的整数常量 "
                f"{expected_value}"
            ),
            repair_action=f"将 {clause_name} 精确改为整数常量 {expected_value}。",
            details={"clause": clause_name, "expected_value": expected_value},
        ) from error
    if value < 0:
        raise SqlValidationError(
            code="negative_pagination_value",
            message=(
                f"SQL 的 {clause_name} 实际值为 {value}，与查询计划指定的 "
                f"{expected_value} 不一致"
            ),
            repair_action=f"将 {clause_name} 精确改为整数常量 {expected_value}。",
            details={
                "clause": clause_name,
                "actual_value": value,
                "expected_value": expected_value,
            },
        )
    return value


# 检查 CTE、基础表、通配字段和高风险函数，确保 AST 仅表达计划允许的只读查询。
def _validate_ast_safety(
    expression: exp.Expression,
    expected_table_names: set[str],
) -> None:
    if not isinstance(expression, exp.Select):
        raise SqlValidationError(
            code="readonly_query_required",
            message="SQL 仅允许 SELECT 或 WITH ... SELECT 查询",
            repair_action="将草稿重写为一条只读 SELECT；需要分步计算时使用 WITH ... SELECT。",
        )
    if expression.args.get("locks"):
        raise SqlValidationError(
            code="locking_read_forbidden",
            message="SQL 不允许使用 FOR UPDATE 或其他锁定读取",
            repair_action="删除 FOR UPDATE、FOR SHARE 等锁定读取子句。",
        )
    if any(True for _ in expression.find_all(exp.Into)):
        raise SqlValidationError(
            code="select_into_forbidden",
            message="SQL 不允许写入文件或其他 SELECT INTO 目标",
            repair_action="删除 INTO 子句，只返回 SELECT 结果集。",
        )
    if any(True for _ in expression.find_all(exp.Star)):
        raise SqlValidationError(
            code="wildcard_forbidden",
            message="SQL 禁止使用 SELECT * 或其他通配字段",
            repair_action="根据查询计划逐项列出需要返回或参与计算的真实字段。",
        )

    cte_names = {cte.alias_or_name for cte in expression.find_all(exp.CTE)}
    physical_table_names: set[str] = set()
    for table in expression.find_all(exp.Table):
        if table.db or table.catalog:
            raise SqlValidationError(
                code="qualified_table_forbidden",
                message="SQL 不允许指定数据库或目录名",
                repair_action="删除数据库或目录前缀，仅使用查询计划声明的原始表名。",
            )
        if table.name not in cte_names:
            physical_table_names.add(table.name)
    if physical_table_names != expected_table_names:
        missing_tables = sorted(expected_table_names - physical_table_names)
        unexpected_tables = sorted(physical_table_names - expected_table_names)
        table_repair_steps: list[str] = []
        if missing_tables:
            table_repair_steps.append(f"补充缺失表 {missing_tables}")
        if unexpected_tables:
            table_repair_steps.append(f"删除额外表 {unexpected_tables}")
        table_repair_action = "；".join(table_repair_steps)
        raise SqlValidationError(
            code="table_scope_mismatch",
            message="SQL 读取的基础表必须与查询计划中的表完全一致",
            repair_action=(
                f"在 SQL 中{table_repair_action}，使基础表集合精确等于 "
                f"{sorted(expected_table_names)}。"
            ),
            details={
                "expected_tables": sorted(expected_table_names),
                "actual_tables": sorted(physical_table_names),
                "missing_tables": missing_tables,
                "unexpected_tables": unexpected_tables,
            },
        )

    for function in expression.find_all(exp.Anonymous):
        if function.name.upper() in FORBIDDEN_FUNCTION_NAMES:
            forbidden_function_name = function.name.upper()
            raise SqlValidationError(
                code="forbidden_function",
                message=f"SQL 包含不允许的函数：{forbidden_function_name}",
                repair_action=(
                    f"删除函数调用 `{forbidden_function_name}(...)`；"
                    "需要返回对应信息时，只选择查询计划中的真实表字段。"
                ),
                details={"function_name": forbidden_function_name},
            )
    if any(True for _ in expression.find_all(exp.CurrentSchema, exp.CurrentUser)):
        raise SqlValidationError(
            code="current_identity_forbidden",
            message="SQL 不允许读取当前数据库或连接身份信息",
            repair_action="删除当前数据库、当前用户或连接身份相关表达式。",
        )


# 判断字面量最近所属的是筛选谓词还是 SELECT 投影，避免把 EXISTS 的 SELECT 1 误判为外部值。
def _is_filter_predicate_literal(literal: exp.Literal) -> bool:
    current = literal.parent
    while current is not None:
        if isinstance(current, (exp.Where, exp.Having, exp.Join)):
            return True
        if isinstance(current, exp.Select):
            return False
        current = current.parent
    return False


# 拒绝 WHERE、HAVING 和 JOIN ON 中的硬编码标量，确保用户或业务筛选值只能经参数绑定进入 SQL。
def _validate_external_filter_values_parameterized(expression: exp.Expression) -> None:
    literal_locations: list[dict[str, str]] = []
    for literal in expression.find_all(exp.Literal):
        if not _is_filter_predicate_literal(literal):
            continue
        predicate = literal.parent
        while predicate is not None and not isinstance(
            predicate,
            (exp.Predicate, exp.Join),
        ):
            predicate = predicate.parent
        literal_locations.append(
            {
                "value": literal.sql(dialect="mysql"),
                "predicate": (
                    predicate.sql(dialect="mysql")
                    if predicate is not None
                    else literal.parent.sql(dialect="mysql")
                ),
            }
        )
    if literal_locations:
        limited_locations = literal_locations[:10]
        location_descriptions = [
            f"`{item['predicate']}` 中的 `{item['value']}`"
            for item in limited_locations
        ]
        raise SqlValidationError(
            code="external_literal_not_parameterized",
            message="SQL 的筛选条件包含未参数化的外部值",
            repair_action=(
                "逐一修正以下位置："
                + "；".join(location_descriptions)
                + "。只替换指出的筛选字面量，不要改动比较运算符：为每个值使用"
                " :parameter_name 命名占位符，并在 parameters 中提供同名 JSON 标量。"
            ),
            details={"literal_locations": limited_locations},
        )


# 将 SQL 草稿中的命名占位符替换为绑定标量，仅用于 AST 语义比较，不生成执行 SQL。
def _resolve_draft_parameter_ast(
    expression: exp.Expression,
    parameters: list[SqlQueryParameter],
) -> exp.Expression:
    parameter_values = {parameter.name: parameter.value for parameter in parameters}

    # 只替换已声明命名占位符；参数缺失仍由后续统一参数校验产生精确错误。
    def replace_placeholder(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Placeholder) and node.name in parameter_values:
            return exp.convert(parameter_values[node.name])
        return node

    return expression.copy().transform(replace_placeholder)


# 将表达式列限定符中的表别名还原为真实表名，使量词语义比较不依赖规划或生成器命名风格。
def _normalize_column_table_aliases(
    expression: exp.Expression,
    declared_aliases: dict[str, str] | None = None,
) -> exp.Expression:
    alias_to_table_name = dict(declared_aliases or {})
    alias_to_table_name.update({
        table.alias: table.name
        for table in expression.find_all(exp.Table)
        if table.alias and table.alias != table.name
    })

    # 只改写列的限定符，不改变表节点和 SQL 执行文本。
    def replace_column_alias(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and node.table in alias_to_table_name:
            node.set("table", exp.to_identifier(alias_to_table_name[node.table]))
        return node

    return expression.copy().transform(replace_column_alias)


# 收集完整 SQL 中每个显式或隐式表别名的真实表名，供嵌套子查询统一还原限定符。
def _build_sql_alias_mapping(expression: exp.Expression) -> dict[str, str]:
    return {
        table.alias_or_name: table.name
        for table in expression.find_all(exp.Table)
    }


# 把简单比较谓词转换为逻辑反条件，用于验证 all 量词的 NOT EXISTS 反例查询。
def _build_predicate_complement(predicate: exp.Expression) -> exp.Expression:
    complement_types: dict[type[exp.Expression], type[exp.Expression]] = {
        exp.EQ: exp.NEQ,
        exp.NEQ: exp.EQ,
        exp.GT: exp.LTE,
        exp.GTE: exp.LT,
        exp.LT: exp.GTE,
        exp.LTE: exp.GT,
    }
    complement_type = complement_types.get(type(predicate))
    if complement_type is None:
        return exp.Not(this=predicate.copy())
    return complement_type(
        this=predicate.this.copy(),
        expression=predicate.expression.copy(),
    )


# 将数值字面量归一化为相同十进制文本，避免 1、1.0 和 1.0000 被误判成不同业务阈值。
def _normalize_numeric_literals(expression: exp.Expression) -> exp.Expression:
    # 仅改写 SQL 数值节点，字符串中的数字仍保持其原始业务含义。
    def replace_numeric_literal(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Literal) or not node.is_number:
            return node
        try:
            normalized_value = Decimal(node.this).normalize()
        except (InvalidOperation, TypeError, ValueError):
            return node
        normalized_text = format(normalized_value, "f")
        if "." in normalized_text:
            normalized_text = normalized_text.rstrip("0").rstrip(".")
        return exp.Literal.number(normalized_text or "0")

    return expression.copy().transform(replace_numeric_literal)


# 使用归一化 MySQL 文本比较两个表达式子树，忽略排版和等值数值精度差异。
def _expressions_are_equivalent(
    left: exp.Expression,
    right: exp.Expression,
) -> bool:
    normalized_left = _normalize_numeric_literals(left)
    normalized_right = _normalize_numeric_literals(right)
    if normalized_left.sql(dialect="mysql") == normalized_right.sql(
        dialect="mysql"
    ):
        return True
    if isinstance(normalized_left, (exp.EQ, exp.NEQ)) and isinstance(
        normalized_right,
        type(normalized_left),
    ):
        return (
            normalized_left.this.sql(dialect="mysql")
            == normalized_right.expression.sql(dialect="mysql")
            and normalized_left.expression.sql(dialect="mysql")
            == normalized_right.this.sql(dialect="mysql")
        )
    return False


# 解析规划量词中的单条 MySQL 条件，并把不可执行计划明确归还查询规划层修复。
def _parse_quantified_plan_condition(
    condition_text: str,
    condition_index: int,
    field_name: str,
) -> exp.Expression:
    try:
        condition_select = parse_one(
            f"SELECT 1 WHERE {condition_text}",
            read="mysql",
        )
    except ParseError as error:
        raise SqlValidationError(
            code="invalid_quantified_predicate",
            message=f"查询计划中的量词字段 {field_name} 不是有效 MySQL 表达式",
            repair_action=(
                "返回查询规划阶段，把 quantified_conditions"
                f"[{condition_index}].{field_name} 改为真实字段组成的单一 MySQL 条件。"
            ),
            retry_target="query_planning",
            details={"condition_index": condition_index, "field_name": field_name},
        ) from error
    condition_where = condition_select.args.get("where")
    if condition_where is None:
        raise SqlValidationError(
            code="invalid_quantified_predicate",
            message=f"查询计划中的量词字段 {field_name} 为空",
            repair_action=(
                "返回查询规划阶段，为 quantified_conditions"
                f"[{condition_index}].{field_name} 提供真实字段条件。"
            ),
            retry_target="query_planning",
            details={"condition_index": condition_index, "field_name": field_name},
        )
    return condition_where.this


# 判断一个子表达式是否包含与规划条件等价的完整谓词，比较前统一还原 SQL 表别名。
def _expression_contains_condition(
    expression: exp.Expression,
    expected_condition: exp.Expression,
    sql_aliases: dict[str, str],
) -> bool:
    return any(
        _expressions_are_equivalent(
            _normalize_column_table_aliases(candidate, sql_aliases),
            expected_condition,
        )
        for candidate in expression.walk()
    )


# 解析规划中的跨表关联条件；无效条件必须回到规划层修正，不能要求 SQL 模型猜测真实关系。
def _parse_planned_join_condition(
    condition_text: str,
    condition_index: int,
) -> exp.Expression:
    try:
        condition_select = parse_one(
            f"SELECT 1 WHERE {condition_text}",
            read="mysql",
        )
    except ParseError as error:
        raise SqlValidationError(
            code="invalid_planned_join_condition",
            message=f"查询计划中的关联条件 joins[{condition_index}].condition 不是有效 MySQL 表达式",
            repair_action=(
                "返回查询规划阶段，把 "
                f"joins[{condition_index}].condition 改为真实字段组成的单一关联条件。"
            ),
            retry_target="query_planning",
            details={
                "join_index": condition_index,
                "condition": condition_text,
            },
        ) from error
    condition_where = condition_select.args.get("where")
    if condition_where is None:
        raise SqlValidationError(
            code="invalid_planned_join_condition",
            message=f"查询计划中的关联条件 joins[{condition_index}].condition 为空",
            repair_action=(
                "返回查询规划阶段，为 "
                f"joins[{condition_index}].condition 提供真实字段关联条件。"
            ),
            retry_target="query_planning",
            details={
                "join_index": condition_index,
                "condition": condition_text,
            },
        )
    return condition_where.this


# 只遍历当前 SELECT 自己的表达式，遇到嵌套 SELECT 即停止，避免无关子查询冒充外层关联。
def _walk_without_nested_selects(
    expression: exp.Expression,
) -> list[exp.Expression]:
    scoped_expressions: list[exp.Expression] = [expression]
    for child in expression.iter_expressions():
        if isinstance(child, exp.Select):
            continue
        scoped_expressions.extend(_walk_without_nested_selects(child))
    return scoped_expressions


# 判断关联条件是否位于当前 SELECT 的 JOIN、WHERE 或 HAVING 中，嵌套查询中的同名条件不计入。
def _select_scope_contains_condition(
    select_expression: exp.Select,
    expected_condition: exp.Expression,
    sql_aliases: dict[str, str],
) -> bool:
    condition_roots: list[exp.Expression] = []
    for join_expression in select_expression.args.get("joins") or []:
        on_expression = join_expression.args.get("on")
        if on_expression is not None:
            condition_roots.append(on_expression)
    for clause_name in ("where", "having"):
        clause = select_expression.args.get(clause_name)
        if clause is not None and clause.this is not None:
            condition_roots.append(clause.this)
    return any(
        _expressions_are_equivalent(
            _normalize_column_table_aliases(candidate, sql_aliases),
            expected_condition,
        )
        for condition_root in condition_roots
        for candidate in _walk_without_nested_selects(condition_root)
    )


# 解析查询块中的普通筛选或 HAVING 条件，无法解析时明确返回规划阶段修正。
def _parse_query_block_condition(
    condition_text: str,
    block_id: str,
    component_name: str,
    component_index: int,
) -> exp.Expression:
    try:
        condition_select = parse_one(f"SELECT 1 WHERE {condition_text}", read="mysql")
    except ParseError as error:
        field_path = (
            f"query_blocks[{block_id}].{component_name}[{component_index}].condition"
        )
        raise SqlValidationError(
            code="invalid_query_block_condition",
            message=f"查询计划中的条件 {field_path} 不是有效 MySQL 表达式",
            repair_action=(
                f"返回查询规划阶段，把 {field_path} 改为真实字段组成的单一条件。"
            ),
            retry_target="query_planning",
            details={"field_path": field_path, "condition": condition_text},
        ) from error
    where_expression = condition_select.args.get("where")
    if where_expression is None:
        raise SqlValidationError(
            code="invalid_query_block_condition",
            message=f"查询块 {block_id} 的 {component_name}[{component_index}] 条件为空",
            repair_action="返回查询规划阶段，为该计划组件提供真实字段条件。",
            retry_target="query_planning",
        )
    return where_expression.this


# 解析查询块的字段或聚合表达式，保证 AST 审计不依赖字符串片段匹配。
def _parse_query_block_value_expression(
    expression_text: str,
    block_id: str,
    field_path: str,
) -> exp.Expression:
    try:
        select_expression = parse_one(f"SELECT {expression_text}", read="mysql")
    except ParseError as error:
        raise SqlValidationError(
            code="invalid_query_block_expression",
            message=f"查询块 {block_id} 的 {field_path} 不是有效 MySQL 表达式",
            repair_action=(
                f"返回查询规划阶段，把 query_blocks[{block_id}].{field_path} "
                "改为真实字段组成的单一表达式。"
            ),
            retry_target="query_planning",
        ) from error
    if not select_expression.expressions:
        raise SqlValidationError(
            code="invalid_query_block_expression",
            message=f"查询块 {block_id} 的 {field_path} 为空",
            repair_action="返回查询规划阶段，为该字段提供真实 SQL 表达式。",
            retry_target="query_planning",
        )
    return select_expression.expressions[0]


# 只在指定 SELECT 子句中比较条件，防止 WHERE 与 HAVING 被模型互相挪用。
def _select_clause_contains_condition(
    select_expression: exp.Select,
    clause_name: str,
    expected_condition: exp.Expression,
    sql_aliases: dict[str, str] | None = None,
) -> bool:
    clause = select_expression.args.get(clause_name)
    if clause is None or clause.this is None:
        return False
    return any(
        _expressions_are_equivalent(
            _normalize_column_table_aliases(candidate, sql_aliases),
            expected_condition,
        )
        for candidate in _walk_without_nested_selects(clause.this)
    )


# 提取根 SELECT 与同名 CTE 对应的查询块作用域，并拒绝 SQL 擅自增删查询块。
def _get_query_block_selects(
    expression: exp.Expression,
    query_plan: NaturalLanguageQueryPlan,
) -> dict[str, exp.Select]:
    root_select = expression if isinstance(expression, exp.Select) else expression.find(exp.Select)
    if not isinstance(root_select, exp.Select):
        raise SqlValidationError(
            code="query_block_root_missing",
            message="SQL 没有可作为根查询块的 SELECT",
            repair_action="按 root_block_id 生成最外层 SELECT，并完整实现根查询块。",
        )
    ctes_by_name: dict[str, exp.CTE] = {}
    with_expression = root_select.args.get("with") or root_select.args.get("with_")
    if with_expression is not None:
        for cte in with_expression.expressions:
            cte_name = cte.alias_or_name
            if cte_name in ctes_by_name:
                raise SqlValidationError(
                    code="query_block_cte_duplicated",
                    message=f"SQL 重复定义查询块 CTE：{cte_name}",
                    repair_action=f"只保留一个名为 `{cte_name}` 的 CTE。",
                )
            ctes_by_name[cte_name] = cte
    expected_cte_order = [
        block.block_id
        for block in query_plan.query_blocks
        if block.block_id != query_plan.root_block_id
    ]
    expected_cte_names = set(expected_cte_order)
    actual_cte_names = set(ctes_by_name)
    if expected_cte_names != actual_cte_names:
        missing_names = sorted(expected_cte_names - actual_cte_names)
        extra_names = sorted(actual_cte_names - expected_cte_names)
        raise SqlValidationError(
            code="query_block_cte_mismatch",
            message="SQL 的 CTE 集合与查询计划 query_blocks 不一致",
            repair_action=(
                "为每个非根 query_block 生成且只生成一个同名 CTE；"
                f"缺少：{missing_names or '无'}；多余：{extra_names or '无'}。"
            ),
            details={"missing_blocks": missing_names, "extra_blocks": extra_names},
        )
    actual_cte_order = list(ctes_by_name)
    if actual_cte_order != expected_cte_order:
        raise SqlValidationError(
            code="query_block_cte_order_mismatch",
            message="SQL 的 CTE 顺序与查询计划拓扑顺序不一致",
            repair_action=(
                "按 query_blocks 的依赖顺序重新排列 CTE："
                + "、".join(expected_cte_order)
                + "。"
            ),
            details={
                "expected_order": expected_cte_order,
                "actual_order": actual_cte_order,
            },
        )
    block_selects = {query_plan.root_block_id: root_select}
    for block_id, cte in ctes_by_name.items():
        cte_select = cte.this if isinstance(cte.this, exp.Select) else cte.this.find(exp.Select)
        if not isinstance(cte_select, exp.Select):
            raise SqlValidationError(
                code="query_block_select_missing",
                message=f"查询块 {block_id} 没有可执行的 SELECT",
                repair_action=f"将 CTE `{block_id}` 改为一条 SELECT 查询。",
            )
        block_selects[block_id] = cte_select
    return block_selects


# 判断一个 AST 节点归属于哪个命名查询块；块内部的普通相关子查询仍归所属查询块。
def _find_node_query_block(
    node: exp.Expression,
    block_selects: dict[str, exp.Select],
) -> str | None:
    selects_by_identity = {id(select): block_id for block_id, select in block_selects.items()}
    current: exp.Expression | None = node
    while current is not None:
        if isinstance(current, exp.Select) and id(current) in selects_by_identity:
            return selects_by_identity[id(current)]
        current = current.parent
    return None


# 读取查询块实际拥有的真实表与前置 CTE，嵌套相关子查询不会被错误分配给根块。
def _get_query_block_sources(
    block_id: str,
    select_expression: exp.Select,
    block_selects: dict[str, exp.Select],
    all_block_ids: set[str],
) -> tuple[set[str], set[str]]:
    physical_tables: set[str] = set()
    input_blocks: set[str] = set()
    for table in select_expression.find_all(exp.Table):
        if _find_node_query_block(table, block_selects) != block_id:
            continue
        if table.name in all_block_ids:
            input_blocks.add(table.name)
        else:
            physical_tables.add(table.name)
    return physical_tables, input_blocks


# 校验查询块输出别名、数据源和关联条件均落实在其自身 AST 作用域。
def _validate_query_block_implementation(
    expression: exp.Expression,
    query_plan: NaturalLanguageQueryPlan,
) -> dict[str, exp.Select]:
    block_selects = _get_query_block_selects(expression, query_plan)
    all_block_ids = set(block_selects)
    for block in query_plan.query_blocks:
        block_select = block_selects[block.block_id]
        comparison_aliases = (
            {}
            if block.aliases
            else _build_sql_alias_mapping(block_select)
        )
        actual_tables, actual_inputs = _get_query_block_sources(
            block.block_id,
            block_select,
            block_selects,
            all_block_ids,
        )
        expected_tables = set(block.source_tables)
        expected_inputs = set(block.input_blocks)
        if actual_tables != expected_tables or actual_inputs != expected_inputs:
            raise SqlValidationError(
                code="query_block_source_mismatch",
                message=f"SQL 查询块 {block.block_id} 读取的数据源与计划不一致",
                repair_action=(
                    f"查询块 `{block.block_id}` 只能读取真实表 {sorted(expected_tables)} "
                    f"和前置块 {sorted(expected_inputs)}；当前真实表 {sorted(actual_tables)}，"
                    f"当前前置块 {sorted(actual_inputs)}。"
                ),
                details={
                    "block_id": block.block_id,
                    "expected_tables": sorted(expected_tables),
                    "actual_tables": sorted(actual_tables),
                    "expected_input_blocks": sorted(expected_inputs),
                    "actual_input_blocks": sorted(actual_inputs),
                },
            )
        if block.aliases:
            actual_role_aliases = {
                table.alias_or_name
                for table in block_select.find_all(exp.Table)
                if _find_node_query_block(table, block_selects) == block.block_id
                and table.name not in all_block_ids
            }
            aliased_source_tables = {alias.source_table for alias in block.aliases}
            expected_role_aliases = {alias.alias for alias in block.aliases} | (
                set(block.source_tables) - aliased_source_tables
            )
            if actual_role_aliases != expected_role_aliases:
                raise SqlValidationError(
                    code="query_block_alias_mismatch",
                    message=f"SQL 查询块 {block.block_id} 没有按计划使用角色别名",
                    repair_action=(
                        f"查询块 `{block.block_id}` 必须精确使用角色别名 "
                        f"{sorted(expected_role_aliases)}；当前为 "
                        f"{sorted(actual_role_aliases)}。"
                    ),
                    details={
                        "block_id": block.block_id,
                        "expected_aliases": sorted(expected_role_aliases),
                        "actual_aliases": sorted(actual_role_aliases),
                    },
                )
        expected_outputs = [field.result_field for field in block.select_fields]
        actual_outputs = [item.alias_or_name for item in block_select.expressions]
        if actual_outputs != expected_outputs:
            raise SqlValidationError(
                code="query_block_output_mismatch",
                message=f"SQL 查询块 {block.block_id} 的输出列与计划不一致",
                repair_action=(
                    f"按 select_fields 顺序为查询块 `{block.block_id}` 输出并显式命名："
                    + "、".join(expected_outputs)
                    + "。"
                ),
                details={
                    "block_id": block.block_id,
                    "expected_outputs": expected_outputs,
                    "actual_outputs": actual_outputs,
                },
            )
        for field_index, (planned_field, actual_field) in enumerate(
            zip(block.select_fields, block_select.expressions, strict=True)
        ):
            expected_field_expression = _parse_query_block_value_expression(
                planned_field.field,
                block.block_id,
                f"select_fields[{field_index}].field",
            )
            actual_field_expression = (
                actual_field.this if isinstance(actual_field, exp.Alias) else actual_field
            )
            if _expressions_are_equivalent(
                _normalize_column_table_aliases(
                    actual_field_expression,
                    comparison_aliases,
                ),
                expected_field_expression,
            ):
                continue
            raise SqlValidationError(
                code="query_block_select_expression_mismatch",
                message=(
                    f"查询块 {block.block_id} 的输出 {planned_field.result_field} "
                    "没有使用计划字段表达式"
                ),
                repair_action=(
                    f"将查询块 `{block.block_id}` 的 `{planned_field.result_field}` "
                    f"精确改为 `{planned_field.field} AS {planned_field.result_field}`。"
                ),
                details={
                    "block_id": block.block_id,
                    "field_index": field_index,
                    "expected_expression": planned_field.field,
                },
            )
        actual_group = block_select.args.get("group")
        actual_group_expressions = list(actual_group.expressions) if actual_group else []
        expected_group_expressions = [
            _parse_query_block_value_expression(
                field_name,
                block.block_id,
                f"group_by[{group_index}]",
            )
            for group_index, field_name in enumerate(block.group_by)
        ]
        if len(actual_group_expressions) != len(expected_group_expressions) or any(
            not _expressions_are_equivalent(
                _normalize_column_table_aliases(actual, comparison_aliases),
                expected,
            )
            for actual, expected in zip(
                actual_group_expressions,
                expected_group_expressions,
                strict=False,
            )
        ):
            raise SqlValidationError(
                code="query_block_group_by_mismatch",
                message=f"查询块 {block.block_id} 的 GROUP BY 与计划粒度不一致",
                repair_action=(
                    f"将查询块 `{block.block_id}` 的 GROUP BY 按顺序精确改为："
                    + ("、".join(block.group_by) or "无 GROUP BY")
                    + "。"
                ),
                details={"block_id": block.block_id, "expected_group_by": block.group_by},
            )
        for aggregation_index, aggregation in enumerate(block.aggregations):
            expected_aggregation = _parse_query_block_value_expression(
                aggregation.expression,
                block.block_id,
                f"aggregations[{aggregation_index}].expression",
            )
            if any(
                _expressions_are_equivalent(
                    _normalize_column_table_aliases(candidate, comparison_aliases),
                    expected_aggregation,
                )
                for candidate in _walk_without_nested_selects(block_select)
            ):
                continue
            raise SqlValidationError(
                code="query_block_aggregation_missing",
                message=(
                    f"查询块 {block.block_id} 没有实现计划聚合："
                    f"{aggregation.expression}"
                ),
                repair_action=(
                    f"只在查询块 `{block.block_id}` 中加入聚合表达式 "
                    f"`{aggregation.expression}`，不得移动到其他查询块。"
                ),
                details={
                    "block_id": block.block_id,
                    "aggregation_index": aggregation_index,
                    "expression": aggregation.expression,
                },
            )
        actual_order = block_select.args.get("order")
        actual_order_expressions = list(actual_order.expressions) if actual_order else []
        order_mismatch = len(actual_order_expressions) != len(block.order_by)
        if not order_mismatch:
            for order_index, (actual_order_item, planned_order_item) in enumerate(
                zip(actual_order_expressions, block.order_by, strict=True)
            ):
                expected_order_field = _parse_query_block_value_expression(
                    planned_order_item.field,
                    block.block_id,
                    f"order_by[{order_index}].field",
                )
                actual_order_field = (
                    actual_order_item.this
                    if isinstance(actual_order_item, exp.Ordered)
                    else actual_order_item
                )
                actual_direction = (
                    "DESC"
                    if isinstance(actual_order_item, exp.Ordered)
                    and bool(actual_order_item.args.get("desc"))
                    else "ASC"
                )
                if not _expressions_are_equivalent(
                    _normalize_column_table_aliases(
                        actual_order_field,
                        comparison_aliases,
                    ),
                    expected_order_field,
                ) or actual_direction != planned_order_item.direction:
                    order_mismatch = True
                    break
        if order_mismatch:
            expected_order = [
                f"{item.field} {item.direction}" for item in block.order_by
            ]
            raise SqlValidationError(
                code="query_block_order_by_mismatch",
                message=f"查询块 {block.block_id} 的 ORDER BY 与计划不一致",
                repair_action=(
                    f"将查询块 `{block.block_id}` 的 ORDER BY 按顺序精确改为："
                    + ("、".join(expected_order) or "无 ORDER BY")
                    + "。"
                ),
                details={"block_id": block.block_id, "expected_order_by": expected_order},
            )
        for join_index, planned_join in enumerate(block.joins):
            expected_condition = _parse_planned_join_condition(
                planned_join.condition,
                join_index,
            )
            if _select_scope_contains_condition(
                block_select,
                expected_condition,
                comparison_aliases,
            ):
                continue
            raise SqlValidationError(
                code="query_block_join_wrong_scope",
                message=(
                    f"查询计划中的关联没有在指定查询块 {block.block_id} 内实现："
                    f"{planned_join.condition}"
                ),
                repair_action=(
                    f"只在查询块 `{block.block_id}` 的 JOIN ON、WHERE 或 HAVING 中加入 "
                    f"`{planned_join.condition}`，不得移动到其他 CTE 或根查询块。"
                ),
                details={
                    "block_id": block.block_id,
                    "join_index": join_index,
                    "condition": planned_join.condition,
                },
            )
        for component_name, planned_conditions, clause_name in (
            ("filters", block.filters, "where"),
            ("having", block.having, "having"),
        ):
            for condition_index, planned_condition in enumerate(planned_conditions):
                expected_condition = _parse_query_block_condition(
                    planned_condition.condition,
                    block.block_id,
                    component_name,
                    condition_index,
                )
                if _select_clause_contains_condition(
                    block_select,
                    clause_name,
                    expected_condition,
                    comparison_aliases,
                ):
                    continue
                raise SqlValidationError(
                    code="query_block_condition_wrong_scope",
                    message=(
                        f"查询计划条件没有在查询块 {block.block_id} 的 "
                        f"{clause_name.upper()} 中实现：{planned_condition.condition}"
                    ),
                    repair_action=(
                        f"只在查询块 `{block.block_id}` 的 {clause_name.upper()} 中加入 "
                        f"`{planned_condition.condition}`，不得移动到其他查询块或子句。"
                    ),
                    details={
                        "block_id": block.block_id,
                        "component": component_name,
                        "condition_index": condition_index,
                        "condition": planned_condition.condition,
                    },
                )
    return block_selects


# 校验相关子查询中的主体关联确实跨越内外层作用域，拒绝只关联两个内层表的伪相关条件。
def _exists_contains_outer_correlation(
    exists_expression: exp.Exists,
    expected_correlation: exp.Expression,
    sql_aliases: dict[str, str],
) -> bool:
    local_select = exists_expression.this
    local_aliases = {
        table.alias_or_name
        for table in local_select.find_all(exp.Table)
        if table.find_ancestor(exp.Select) is local_select
    }
    for candidate in local_select.find_all(exp.EQ):
        normalized_candidate = _normalize_column_table_aliases(
            candidate,
            sql_aliases,
        )
        if not _expressions_are_equivalent(
            normalized_candidate,
            expected_correlation,
        ):
            continue
        candidate_columns = list(candidate.find_all(exp.Column))
        if len(candidate_columns) != 2:
            continue
        local_flags = [
            bool(column.table) and column.table in local_aliases
            for column in candidate_columns
        ]
        if any(local_flags) and not all(local_flags):
            return True
    return False


# 要求成员条件、集合范围和内外层关联同时出现在同一个正确极性的 EXISTS 子查询中。
def _exists_tree_satisfies_quantified_contract(
    expression: exp.Expression,
    expected_predicate: exp.Expression,
    expected_correlation: exp.Expression | None,
    expected_collection_filters: list[exp.Expression],
    require_not_exists: bool,
    block_id: str | None = None,
    block_selects: dict[str, exp.Select] | None = None,
    preserve_declared_aliases: bool = False,
) -> bool:
    sql_aliases = (
        {} if preserve_declared_aliases else _build_sql_alias_mapping(expression)
    )
    for exists_expression in expression.find_all(exp.Exists):
        if (
            block_id is not None
            and block_selects is not None
            and _find_node_query_block(exists_expression, block_selects) != block_id
        ):
            continue
        parent = exists_expression.parent
        is_not_exists = isinstance(parent, exp.Not)
        if is_not_exists != require_not_exists:
            continue
        if not _expression_contains_condition(
            exists_expression,
            expected_predicate,
            sql_aliases,
        ):
            continue
        if expected_correlation is not None and not _exists_contains_outer_correlation(
            exists_expression,
            expected_correlation,
            sql_aliases,
        ):
            continue
        if not all(
            _expression_contains_condition(
                exists_expression,
                collection_filter,
                sql_aliases,
            )
            for collection_filter in expected_collection_filters
        ):
            continue
        return True
    return False


# 对 EXISTS/NOT EXISTS 量词执行 AST 语义校验，防止运算符变化把 all、any、none 查询反转。
def _validate_quantified_condition_implementation(
    expression: exp.Expression,
    draft: SqlQueryDraft,
    query_plan: NaturalLanguageQueryPlan,
) -> None:
    resolved_expression = _resolve_draft_parameter_ast(
        expression,
        draft.parameters,
    )
    block_selects = _get_query_block_selects(resolved_expression, query_plan)
    for block, index, condition in query_plan.iter_quantified_conditions():
        comparison_aliases = (
            {} if block.aliases else _build_sql_alias_mapping(block_selects[block.block_id])
        )
        expected_predicate = _parse_quantified_plan_condition(
            condition.predicate,
            index,
            "predicate",
        )
        expected_correlation = (
            _parse_quantified_plan_condition(
                condition.correlation_condition,
                index,
                "correlation_condition",
            )
            if condition.correlation_condition is not None
            else None
        )
        expected_collection_filters = [
            _parse_quantified_plan_condition(
                collection_filter,
                index,
                f"collection_filters[{filter_index}]",
            )
            for filter_index, collection_filter in enumerate(
                condition.collection_filters
            )
        ]
        if condition.implementation_hint == "having":
            missing_having_contract: list[str] = []
            for label, expected_expression in (
                [("predicate", expected_predicate)]
                + [
                    (f"collection_filters[{filter_index}]", collection_filter)
                    for filter_index, collection_filter in enumerate(
                        expected_collection_filters
                    )
                ]
            ):
                if any(
                    _expressions_are_equivalent(
                        _normalize_column_table_aliases(candidate, comparison_aliases),
                        expected_expression,
                    )
                    for candidate in _walk_without_nested_selects(
                        block_selects[block.block_id]
                    )
                ):
                    continue
                missing_having_contract.append(label)
            if not missing_having_contract:
                continue
            raise SqlValidationError(
                code="quantified_having_contract_missing",
                message=(
                    f"查询块 {block.block_id} 的 HAVING 量词没有完整实现成员条件和集合范围"
                ),
                repair_action=(
                    f"只在查询块 `{block.block_id}` 中补充缺失的量词组件："
                    + "、".join(missing_having_contract)
                    + "。predicate 可放入条件聚合或适用的成员筛选；"
                    "collection_filters 必须限定本块参与量化的成员集合。"
                ),
                details={
                    "block_id": block.block_id,
                    "condition_index": index,
                    "missing_components": missing_having_contract,
                },
            )
        if condition.implementation_hint not in {"exists", "not_exists"}:
            continue
        require_not_exists = condition.implementation_hint == "not_exists"
        if condition.quantifier == "all" and require_not_exists:
            expected_predicate = _build_predicate_complement(expected_predicate)
        elif condition.quantifier == "none" and require_not_exists:
            expected_predicate = expected_predicate.copy()
        elif condition.quantifier == "any" and not require_not_exists:
            expected_predicate = expected_predicate.copy()
        else:
            continue
        if _exists_tree_satisfies_quantified_contract(
            block_selects[block.block_id],
            expected_predicate,
            expected_correlation,
            expected_collection_filters,
            require_not_exists,
            block.block_id,
            block_selects,
            bool(block.aliases),
        ):
            continue
        expected_sql = expected_predicate.sql(dialect="mysql")
        exists_keyword = "NOT EXISTS" if require_not_exists else "EXISTS"
        correlation_sql = (
            expected_correlation.sql(dialect="mysql")
            if expected_correlation is not None
            else "无"
        )
        collection_filter_sql = [
            item.sql(dialect="mysql") for item in expected_collection_filters
        ]
        raise SqlValidationError(
            code="quantified_condition_mismatch",
            message=(
                f"SQL 没有正确实现量词 {condition.quantifier} 的成员判断"
            ),
            repair_action=(
                f"在同一个 {exists_keyword} 子查询中同时使用："
                f"内外层关联 `{correlation_sql}`；"
                f"集合范围 {collection_filter_sql or ['无额外条件']}；"
                f"成员条件 `{expected_sql}`。不要把关联改成两个内层表之间的条件。"
            ),
            details={
                "block_id": block.block_id,
                "condition_index": index,
                "quantifier": condition.quantifier,
                "expected_member_condition": expected_sql,
                "expected_correlation_condition": correlation_sql,
                "expected_collection_filters": collection_filter_sql,
                "implementation_hint": condition.implementation_hint,
            },
        )


# 校验 SQL 的 LIMIT 与规划层确定的结果范围完全一致，不追加内部上限。
def _resolve_effective_limit(
    expression: exp.Expression,
    query_plan: NaturalLanguageQueryPlan,
) -> int | None:
    planned_limit = query_plan.pagination.limit
    limit_clause = expression.args.get("limit")
    if planned_limit is None:
        if limit_clause is not None:
            raise SqlValidationError(
                code="unexpected_limit",
                message="查询计划未指定分页时，SQL 不得自行添加 LIMIT",
                repair_action="删除 LIMIT，完整执行查询计划已确认的筛选范围。",
            )
        effective_limit = None
    else:
        actual_limit = _get_integer_clause_value(
            limit_clause,
            "LIMIT",
            planned_limit,
        )
        if limit_clause is None or actual_limit != planned_limit:
            raise SqlValidationError(
                code="limit_mismatch",
                message="SQL 的 LIMIT 必须与查询计划指定的分页上限一致",
                repair_action=f"将 LIMIT 精确改为查询计划指定的 {planned_limit}。",
                details={"planned_limit": planned_limit, "actual_limit": actual_limit},
            )
        effective_limit = actual_limit

    expected_offset = query_plan.pagination.offset
    actual_offset = _get_integer_clause_value(
        expression.args.get("offset"),
        "OFFSET",
        expected_offset,
    )
    if actual_offset != expected_offset:
        raise SqlValidationError(
            code="offset_mismatch",
            message="SQL 的 OFFSET 必须与查询计划一致",
            repair_action=f"将 OFFSET 精确改为查询计划指定的 {expected_offset}。",
            details={"planned_offset": expected_offset, "actual_offset": actual_offset},
        )
    return effective_limit


# 校验结果列名与 SELECT 实际输出名称一一对应，避免 DictCursor 的同名字段覆盖导致展示和审计失真。
def _validate_result_columns(
    expression: exp.Expression,
    result_columns: list[str],
    query_plan: NaturalLanguageQueryPlan,
) -> None:
    if len(result_columns) != len(set(result_columns)):
        duplicate_columns = sorted(
            {
                column_name
                for column_name in result_columns
                if result_columns.count(column_name) > 1
            }
        )
        raise SqlValidationError(
            code="duplicate_result_columns",
            message="result_columns 不能包含重复列名",
            repair_action=(
                f"为重复列 {duplicate_columns} 的每个 SELECT 输出设置不同的 "
                "AS 别名，并按 SELECT 顺序将这些别名写入 result_columns。"
            ),
            details={"duplicate_columns": duplicate_columns},
        )
    actual_result_columns = [
        selected_expression.output_name
        for selected_expression in expression.expressions
    ]
    if actual_result_columns != result_columns:
        raise SqlValidationError(
            code="result_columns_mismatch",
            message="result_columns 必须与 SQL SELECT 输出列名按顺序完全一致",
            repair_action=(
                "将 result_columns 精确改为 SQL SELECT 的实际输出列："
                f"{actual_result_columns}。"
            ),
            details={
                "actual_result_columns": actual_result_columns,
                "declared_result_columns": result_columns,
            },
        )
    expected_result_columns = [
        select_field.result_field for select_field in query_plan.select_fields
    ]
    if result_columns != expected_result_columns:
        raise SqlValidationError(
            code="query_plan_result_columns_mismatch",
            message="SQL 输出列必须与查询计划的 result_field 按顺序完全一致",
            repair_action=(
                "按 query_plan.select_fields 的顺序，为每个 SELECT 表达式显式添加 "
                "AS 别名，并把 result_columns 精确改为："
                f"{expected_result_columns}。"
            ),
            details={
                "expected_result_columns": expected_result_columns,
                "actual_result_columns": result_columns,
            },
        )


# 验证命名占位符与参数对象一一对应，并按 SQL 文本出现顺序编译为 asyncmy 使用的 %s 参数。
def _compile_named_parameters(
    sql: str,
    parameters: list[SqlQueryParameter],
) -> tuple[str, tuple[SqlScalar, ...]]:
    parameter_values_by_name = {
        parameter.name: parameter.value for parameter in parameters
    }
    placeholder_names = [
        placeholder.name
        for placeholder in parse(sql, read="mysql")[0].find_all(exp.Placeholder)
    ]
    if any(not name for name in placeholder_names):
        raise SqlValidationError(
            code="invalid_placeholder",
            message="SQL 只能使用 :名称 形式的命名占位符",
            repair_action="将全部占位符改为 :parameter_name，并在 parameters 中提供同名键。",
        )
    if set(placeholder_names) != set(parameter_values_by_name):
        unique_placeholder_names = set(placeholder_names)
        parameter_names = set(parameter_values_by_name)
        missing_parameters = sorted(unique_placeholder_names - parameter_names)
        unused_parameters = sorted(parameter_names - unique_placeholder_names)
        parameter_repair_steps: list[str] = []
        if missing_parameters:
            parameter_repair_steps.append(
                f"在 parameters 中补充参数 {missing_parameters}"
            )
        if unused_parameters:
            parameter_repair_steps.append(
                f"从 parameters 中删除参数 {unused_parameters}"
            )
        raise SqlValidationError(
            code="parameter_mismatch",
            message="SQL 命名占位符必须与 parameters 的键完全一致",
            repair_action="；".join(parameter_repair_steps) + "。",
            details={
                "placeholder_names": sorted(unique_placeholder_names),
                "parameter_names": sorted(parameter_names),
                "missing_parameters": missing_parameters,
                "unused_parameters": unused_parameters,
            },
        )

    compiled_parts: list[str] = []
    ordered_values: list[SqlScalar] = []
    cursor = 0
    while cursor < len(sql):
        character = sql[cursor]
        if character in ("'", '"', "`"):
            quote = character
            end = cursor + 1
            while end < len(sql):
                if sql[end] == quote:
                    if end + 1 < len(sql) and sql[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                if sql[end] == "\\" and quote != "`" and end + 1 < len(sql):
                    end += 2
                    continue
                end += 1
            compiled_parts.append(sql[cursor:end])
            cursor = end
            continue
        matched_placeholder = re.match(r":([A-Za-z_][A-Za-z0-9_]*)", sql[cursor:])
        if matched_placeholder is None:
            compiled_parts.append(character)
            cursor += 1
            continue
        parameter_name = matched_placeholder.group(1)
        compiled_parts.append("%s")
        ordered_values.append(parameter_values_by_name[parameter_name])
        cursor += len(matched_placeholder.group(0))
    return "".join(compiled_parts), tuple(ordered_values)


# 对模型草稿执行 AST 安全校验、分页约束和参数编译，返回可传给 asyncmy 的 SQL 与参数序列。
def validate_sql_draft(
    draft: SqlQueryDraft,
    query_plan: NaturalLanguageQueryPlan,
    allowed_table_names: frozenset[str],
) -> tuple[str, tuple[SqlScalar, ...]]:
    sql = draft.sql.strip()
    if not sql:
        raise SqlValidationError(
            code="sql_empty",
            message="SQL 不能为空",
            repair_action="根据查询计划生成一条完整的只读 SELECT 查询。",
        )
    _reject_comments_and_statement_separators(sql)
    try:
        expressions = parse(sql, read="mysql")
    except ParseError as error:
        parser_error = str(error)[:240]
        raise SqlValidationError(
            code="invalid_mysql",
            message="SQL 不是有效的 MySQL 查询",
            repair_action=f"根据解析器错误修正 SQL 语法：{parser_error}。",
            details={"parser_error": parser_error},
        ) from error
    if len(expressions) != 1:
        raise SqlValidationError(
            code="multiple_statements",
            message="SQL 必须且只能包含一条语句",
            repair_action="仅保留一条 SELECT，删除其他语句。",
        )
    expression = expressions[0]
    _validate_ast_safety(
        expression,
        set(_get_plan_table_names(query_plan, allowed_table_names)),
    )
    resolved_expression = _resolve_draft_parameter_ast(expression, draft.parameters)
    _validate_query_block_implementation(resolved_expression, query_plan)
    _validate_quantified_condition_implementation(expression, draft, query_plan)
    _validate_external_filter_values_parameterized(expression)
    _validate_result_columns(expression, draft.result_columns, query_plan)
    _resolve_effective_limit(expression, query_plan)
    normalized_sql = expression.sql(dialect="mysql")
    return _compile_named_parameters(normalized_sql, draft.parameters)


class AsyncMyReadOnlySqlExecutor:
    """通过独立的只读事务执行已通过静态校验的参数化 SQL。"""

    # 保存开发数据库配置，执行器只接收已校验的 SQL 和绑定参数而不接触模型输出对象。
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # 使用同步接口包装受超时保护的异步查询，供当前同步 LangGraph 节点调用。
    def execute(
        self,
        sql: str,
        parameters: tuple[SqlScalar, ...],
    ) -> list[dict[str, Any]]:
        try:
            return asyncio.run(
                asyncio.wait_for(
                    self._execute_async(sql, parameters),
                    timeout=QUERY_EXECUTION_TIMEOUT_SECONDS,
                )
            )
        except asyncio.TimeoutError as error:
            raise SqlExecutionError(
                code="sql_execution_timeout",
                message="SQL 查询执行超时",
                repair_action="无需修改 SQL；由系统检查数据库负载或稍后重试。",
                retryable=False,
            ) from error
        except asyncmy.Error as error:
            mysql_code = error.args[0] if error.args and isinstance(error.args[0], int) else None
            raw_message = str(error.args[1] if len(error.args) > 1 else error)
            safe_message = re.sub(
                r"(?i)\b(sk-[A-Za-z0-9_-]+|bearer\s+[A-Za-z0-9._-]+)\b",
                "[REDACTED]",
                raw_message,
            )[:500]
            repairable_codes = {1052, 1054, 1055, 1064, 1111, 1146, 1241, 1242}
            retryable = mysql_code in repairable_codes
            raise SqlExecutionError(
                code=(
                    f"mysql_query_error_{mysql_code}"
                    if mysql_code is not None
                    else "mysql_query_error"
                ),
                message=f"数据库拒绝执行当前只读 SQL：{safe_message}",
                repair_action=(
                    "依据数据库错误和已提供表结构修正 SQL 语法、字段限定、"
                    "分组或子查询写法，保持查询计划业务口径不变。"
                    if retryable
                    else "无需修改 SQL；由系统检查数据库连接、权限或服务状态。"
                ),
                retryable=retryable,
                details={"mysql_error_code": mysql_code},
            ) from error
        except OSError as error:
            raise SqlExecutionError(
                code="database_connection_failed",
                message="无法连接只读数据库",
                repair_action="无需修改 SQL；由系统检查数据库网络和服务状态。",
                retryable=False,
            ) from error

    # 在只读事务中执行参数化查询并回滚事务，避免执行路径意外留下数据库副作用。
    async def _execute_async(
        self,
        sql: str,
        parameters: tuple[SqlScalar, ...],
    ) -> list[dict[str, Any]]:
        connection = await asyncmy.connect(
            host=self._settings.mysql_host,
            port=self._settings.mysql_port,
            user=self._settings.mysql_user,
            password=self._settings.mysql_password.get_secret_value(),
            db=self._settings.mysql_database,
            charset=self._settings.mysql_charset,
            cursor_cls=DictCursor,
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("START TRANSACTION READ ONLY")
                await cursor.execute(sql, parameters)
                return list(await cursor.fetchall())
        finally:
            await connection.rollback()
            connection.close()


class SqlQuerySubgraph:
    """独立执行最终查询计划的生成、分类校验、有限修复和只读执行子图。"""

    # 初始化与规划阶段无关的模型、结构读取器和只读执行器，并构建可回到生成节点的有限重试子图。
    def __init__(
        self,
        client: Any,
        model: str,
        domain_profile: QueryDomainProfile,
        schema_reader: SchemaReader,
        sql_executor: SqlExecutor,
        trace_writer: TraceWriter | None = None,
        progress_emitter: ProgressEmitter | None = None,
        max_tokens: int = DEFAULT_SQL_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model = model
        self._domain_profile = domain_profile
        self._allowed_table_names = frozenset(domain_profile.allowed_tables)
        self._schema_reader = schema_reader
        self._sql_executor = sql_executor
        self._trace_writer = trace_writer
        self._progress_reporter = AgentProgressReporter(
            domain_profile,
            progress_emitter,
        )
        self._max_tokens = max_tokens

        workflow = StateGraph(_SqlQueryState)
        workflow.add_node("generate_sql", self._generate_sql)
        workflow.add_node("validate_sql", self._validate_sql)
        workflow.add_node("execute_sql", self._execute_sql)
        workflow.add_edge(START, "generate_sql")
        workflow.add_conditional_edges(
            "generate_sql",
            self._continue_after_generation,
            {
                "generate_sql": "generate_sql",
                "validate_sql": "validate_sql",
                "end": END,
            },
        )
        workflow.add_conditional_edges(
            "validate_sql",
            self._continue_after_validation,
            {
                "generate_sql": "generate_sql",
                "execute_sql": "execute_sql",
                "end": END,
            },
        )
        workflow.add_conditional_edges(
            "execute_sql",
            self._continue_after_execution,
            {
                "generate_sql": "generate_sql",
                "end": END,
            },
        )
        self._workflow = workflow.compile()

    # 从应用配置创建真实 DeepSeek 客户端、可复用的结构读取器和只读 MySQL 执行器。
    @classmethod
    def from_settings(
        cls,
        domain_profile: QueryDomainProfile,
        settings: Settings | None = None,
        schema_reader: SchemaReader | None = None,
        trace_writer: TraceWriter | None = None,
        progress_emitter: ProgressEmitter | None = None,
    ) -> "SqlQuerySubgraph":
        resolved_settings = settings or get_settings()
        if resolved_settings.deepseek_api_key is None:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法生成最终 SQL 查询")
        client = OpenAI(
            api_key=resolved_settings.deepseek_api_key.get_secret_value(),
            base_url=build_strict_tools_base_url(
                str(resolved_settings.deepseek_base_url)
            ),
            timeout=resolved_settings.deepseek_http_timeout_seconds,
        )
        schema_reader = schema_reader or CachingTableSchemaReader(
            InformationSchemaTableSchemaReader(
                resolved_settings,
                domain_profile.allowed_tables,
            ).read
        ).read
        sql_executor = AsyncMyReadOnlySqlExecutor(resolved_settings)
        return cls(
            client=client,
            model=resolved_settings.deepseek_model,
            domain_profile=domain_profile,
            schema_reader=schema_reader,
            sql_executor=sql_executor.execute,
            trace_writer=trace_writer,
            progress_emitter=progress_emitter,
            max_tokens=resolved_settings.deepseek_query_sql_max_tokens,
        )

    # 在显式启用内部追踪时记录模型输出和校验结果，默认不写入标准输出。
    def _write_trace(self, content: str) -> None:
        if self._trace_writer is not None:
            self._trace_writer(content)

    # 根据查询计划加载每张实际涉及表的结构；任何结构读取失败都会在调用模型前停止。
    def _read_plan_schemas(
        self,
        query_plan: NaturalLanguageQueryPlan,
    ) -> list[TableSchemaToolResponse]:
        schema_results: list[TableSchemaToolResponse] = []
        for table_name in _get_plan_table_names(
            query_plan,
            self._allowed_table_names,
        ):
            schema_result = self._schema_reader(table_name)
            schema_results.append(schema_result)
            if schema_result.status == "failure":
                raise RuntimeError(f"无法读取表 {table_name} 的结构：{schema_result.result}")
        return schema_results

    # 按计划顺序优先采用上游成功结构，缺失时交给共享缓存读取器按需获取并写入缓存。
    def _resolve_plan_schemas(
        self,
        query_plan: NaturalLanguageQueryPlan,
        provided_schema_results: list[TableSchemaToolResponse],
    ) -> list[TableSchemaToolResponse]:
        schema_by_table_name = {
            schema_result.table_name: schema_result
            for schema_result in provided_schema_results
            if schema_result.table_name is not None
        }
        resolved_schema_results: list[TableSchemaToolResponse] = []
        for table_name in _get_plan_table_names(
            query_plan,
            self._allowed_table_names,
        ):
            schema_result = schema_by_table_name.get(table_name)
            if schema_result is None or schema_result.status == "failure":
                schema_result = self._schema_reader(table_name)
            if schema_result.status == "failure":
                raise RuntimeError(
                    f"无法读取表 {table_name} 的结构：{schema_result.result}"
                )
            resolved_schema_results.append(schema_result)
        return resolved_schema_results

    # 强制模型提交 SQL 工具；参数错误写回同一调用 ID，并在预算内重新生成完整草稿。
    def _generate_sql(self, state: _SqlQueryState) -> dict[str, Any]:
        generation_count = state.get("generation_count", 0)
        max_generation_count = state["max_generation_count"]
        raw_model_responses = list(state.get("raw_model_responses", []))
        try:
            schema_results = state.get("schema_results")
            messages = list(state.get("messages", []))
            if schema_results is None:
                provided_schema_results = state.get("provided_schema_results")
                schema_results = (
                    self._read_plan_schemas(state["query_plan"])
                    if provided_schema_results is None
                    else self._resolve_plan_schemas(
                        state["query_plan"], provided_schema_results
                    )
                )
                messages = _build_sql_generation_messages(
                    state["query_plan"], schema_results
                )
            sql_tool = build_sql_query_tool_definition()
            generation_count += 1
            response = self._client.chat.completions.create(
                model=self._model,
                messages=list(messages),
                tools=[sql_tool],
                tool_choice={
                    "type": "function",
                    "function": {"name": SUBMIT_SQL_QUERY_TOOL_NAME},
                },
                **build_non_thinking_completion_options(self._max_tokens),
            )
            raw_model_responses.append(_serialize_raw_response(response))
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            if len(tool_calls) != 1:
                raise ValueError("SQL 生成模型必须且只能调用一次 submit_sql_query")
            tool_call = tool_calls[0]
            if tool_call.function.name != SUBMIT_SQL_QUERY_TOOL_NAME:
                raise ValueError(
                    f"SQL 生成模型调用了未注册工具：{tool_call.function.name}"
                )
            messages.append(message)
            try:
                draft = SqlQueryDraft.model_validate_json(
                    tool_call.function.arguments
                )
            except ValidationError as error:
                messages.append(
                    build_tool_argument_error_message(
                        tool_call.id,
                        SUBMIT_SQL_QUERY_TOOL_NAME,
                        error,
                    )
                )
                error_message = f"SQL 工具参数校验失败：{error}"
                can_retry = generation_count < max_generation_count
                self._write_trace(
                    "\n================ SQL 子图：工具参数失败 ================\n"
                    f"第 {generation_count}/{max_generation_count} 次生成\n"
                    f"{error_message}\n"
                    f"{'将反馈给模型修复。' if can_retry else '已用尽生成次数。'}"
                )
                return {
                    "schema_results": schema_results,
                    "messages": messages,
                    "raw_model_responses": raw_model_responses,
                    "generation_count": generation_count,
                    "current_tool_call_id": tool_call.id,
                    "error": error_message,
                    "error_code": "tool_arguments_validation_failed",
                    "retry_target": "sql_generation",
                    "next_action": "generate_sql" if can_retry else "end",
                }
            self._write_trace(
                "\n================ SQL 子图：生成 ================\n"
                f"第 {generation_count}/{max_generation_count} 次生成\n"
                f"模型工具参数：\n{tool_call.function.arguments}\n"
                f"计划表：{', '.join(table.table_name for table in state['query_plan'].tables)}"
            )
            return {
                "schema_results": schema_results,
                "messages": messages,
                "draft": draft,
                "raw_model_responses": raw_model_responses,
                "generation_count": generation_count,
                "current_tool_call_id": tool_call.id,
                "error": "",
                "error_code": "",
                "retry_target": "none",
                "next_action": "validate_sql",
            }
        except StrictToolSchemaValidationError as error:
            message = f"SQL strict 工具定义不合法：{error}"
            diagnostic = _build_safe_failure_diagnostic(error)
            self._write_trace(
                f"\n================ SQL 子图：工具定义失败 ================\n{message}"
            )
            return {
                "raw_model_responses": raw_model_responses,
                "generation_count": generation_count,
                "error": message,
                "error_code": "invalid_strict_tool_schema",
                "error_diagnostic": diagnostic,
                "retry_target": "none",
                "next_action": "end",
            }
        except SqlValidationError as error:
            message = f"SQL 生成前校验失败：{error}"
            self._write_trace(f"\n================ SQL 子图：生成失败 ================\n{message}")
            return {
                "raw_model_responses": raw_model_responses,
                "generation_count": generation_count,
                "error": message,
                "error_code": error.code,
                "retry_target": error.retry_target,
                "next_action": "end",
            }
        except (RuntimeError, ValueError, IndexError, KeyError) as error:
            message = f"SQL 生成失败：{error}"
            self._write_trace(f"\n================ SQL 子图：生成失败 ================\n{message}")
            return {
                "raw_model_responses": raw_model_responses,
                "generation_count": generation_count,
                "error": message,
                "error_code": "sql_generation_protocol_failed",
                "retry_target": "none",
                "next_action": "end",
            }
        except Exception as error:
            message = "SQL 生成模型调用失败，请稍后重试"
            diagnostic = _build_safe_failure_diagnostic(error)
            self._write_trace(
                "\n================ SQL 子图：生成失败 ================\n"
                f"{message}\n"
                f"异常类型：{diagnostic.exception_type}\n"
                f"HTTP 状态：{diagnostic.status_code or '未知'}\n"
                f"诊断：{diagnostic.message}"
            )
            return {
                "raw_model_responses": raw_model_responses,
                "generation_count": generation_count,
                "error": message,
                "error_code": "sql_generation_request_failed",
                "error_diagnostic": diagnostic,
                "retry_target": "none",
                "next_action": "end",
            }

    # 按生成节点给出的显式动作进入校验、重新生成或终止，避免依赖可能残留的错误字符串判断。
    def _continue_after_generation(
        self,
        state: _SqlQueryState,
    ) -> Literal["generate_sql", "validate_sql", "end"]:
        next_action = state.get("next_action", "end")
        if next_action == "generate_sql":
            return "generate_sql"
        if next_action == "validate_sql":
            return "validate_sql"
        return "end"

    # 在第二节点中执行 AST、表范围、参数和分页校验，绝不让未校验草稿进入执行器。
    def _validate_sql(self, state: _SqlQueryState) -> dict[str, Any]:
        try:
            validated_sql, parameter_values = validate_sql_draft(
                state["draft"],
                state["query_plan"],
                self._allowed_table_names,
            )
            original_expression = parse(state["draft"].sql, read="mysql")[0]
            analysis_sql = original_expression.sql(dialect="mysql")
            self._progress_reporter.emit(
                AgentProgressUpdate(
                    stage="sql_generation",
                    event_type="stage_completed",
                    status="success",
                    title="查询方案已通过安全检查",
                    message="查询只会读取当前业务域允许的数据，正在准备执行。",
                )
            )
            effective_limit = _resolve_effective_limit(
                original_expression,
                state["query_plan"],
            )
            self._write_trace(
                "\n================ SQL 子图：校验通过 ================\n"
                f"实际 SQL：\n{validated_sql}\n"
                f"参数数量：{len(parameter_values)}\n"
                f"规划上限：{effective_limit if effective_limit is not None else '未设置'}"
            )
            return {
                "validated_sql": validated_sql,
                "analysis_sql": analysis_sql,
                "parameter_values": parameter_values,
                "effective_limit": effective_limit,
                "error": "",
                "error_code": "",
                "retry_target": "none",
                "next_action": "execute_sql",
            }
        except SqlValidationError as error:
            message = f"SQL 校验失败：{error}"
            generation_count = state.get("generation_count", 0)
            max_generation_count = state["max_generation_count"]
            can_retry = (
                error.retry_target == "sql_generation"
                and generation_count < max_generation_count
            )
            messages = list(state.get("messages", []))
            tool_call_id = state.get("current_tool_call_id")
            if tool_call_id:
                messages.append(
                    build_sql_validation_error_message(tool_call_id, error)
                )
            self._write_trace(
                "\n================ SQL 子图：校验失败 ================\n"
                f"错误代码：{error.code}\n"
                f"重试归属：{error.retry_target}\n"
                f"{message}\n"
                f"{'将反馈给模型修复。' if can_retry else '不再进行 SQL 模型重试。'}"
            )
            return {
                "messages": messages,
                "error": message,
                "error_code": error.code,
                "retry_target": error.retry_target,
                "next_action": "generate_sql" if can_retry else "end",
            }
        except (ValueError, ParseError, KeyError) as error:
            message = f"SQL 校验失败：{error}"
            self._write_trace(f"\n================ SQL 子图：校验失败 ================\n{message}")
            return {
                "error": message,
                "error_code": "sql_validation_internal_error",
                "retry_target": "none",
                "next_action": "end",
            }

    # 按分类校验结果选择重新生成、执行或终止，规划和基础设施问题不会浪费 SQL 生成预算。
    def _continue_after_validation(
        self,
        state: _SqlQueryState,
    ) -> Literal["generate_sql", "execute_sql", "end"]:
        next_action = state.get("next_action", "end")
        if next_action == "generate_sql":
            return "generate_sql"
        if next_action == "execute_sql":
            return "execute_sql"
        return "end"

    # 在第三节点中执行已绑定参数的只读 SQL，可修复数据库错误按原工具调用 ID 反馈给模型。
    def _execute_sql(self, state: _SqlQueryState) -> dict[str, Any]:
        try:
            self._progress_reporter.emit(
                AgentProgressUpdate(
                    stage="execution",
                    event_type="stage_started",
                    status="running",
                    title="正在读取数据",
                    message="查询方案已经确认，正在从业务数据库读取结果。",
                )
            )
            effective_limit = state["effective_limit"]
            returned_rows = self._sql_executor(
                state["validated_sql"],
                state["parameter_values"],
            )
            rows = returned_rows
            limit_reached = (
                effective_limit is not None and len(rows) == effective_limit
            )
            result = SqlQuerySubgraphResult(
                status="success",
                schema_results=state.get("schema_results", []),
                draft=state["draft"],
                sql=state["validated_sql"],
                analysis_sql=state["analysis_sql"],
                result_columns=state["draft"].result_columns,
                rows=rows,
                planned_limit=state["query_plan"].pagination.limit,
                effective_limit=effective_limit,
                returned_row_count=len(rows),
                limit_reached=limit_reached,
                generation_count=state.get("generation_count", 0),
                max_generation_count=state["max_generation_count"],
                raw_model_response=_serialize_raw_responses(
                    state.get("raw_model_responses", [])
                ),
            )
            self._write_trace(
                "\n================ SQL 子图：执行完成 ================\n"
                f"返回行数：{len(rows)}\n"
                f"规划上限：{effective_limit if effective_limit is not None else '未设置'}\n"
                f"达到行数上限：{'是' if limit_reached else '否'}"
            )
            return {"result": result, "next_action": "end"}
        except SqlExecutionError as error:
            generation_count = state.get("generation_count", 0)
            max_generation_count = state["max_generation_count"]
            can_retry = error.retryable and generation_count < max_generation_count
            messages = list(state.get("messages", []))
            tool_call_id = state.get("current_tool_call_id")
            if tool_call_id:
                messages.append(build_sql_execution_error_message(tool_call_id, error))
            message = f"SQL 执行失败：{error}"
            self._write_trace(
                "\n================ SQL 子图：执行失败 ================\n"
                f"错误代码：{error.code}\n"
                f"{message}\n"
                f"{'将反馈给模型修复。' if can_retry else '不再进行 SQL 模型重试。'}"
            )
            if can_retry:
                return {
                    "messages": messages,
                    "error": message,
                    "error_code": error.code,
                    "retry_target": "sql_generation",
                    "next_action": "generate_sql",
                }
            failed_state = dict(state)
            failed_state.update(
                {
                    "error_code": error.code,
                    "retry_target": "none",
                }
            )
            return {
                "result": self._build_failure_result(failed_state, message),
                "next_action": "end",
            }
        except (RuntimeError, ValueError) as error:
            message = f"SQL 执行失败：{error}"
            self._write_trace(f"\n================ SQL 子图：执行失败 ================\n{message}")
            failed_state = dict(state)
            failed_state.update(
                {
                    "error_code": "sql_execution_failed",
                    "retry_target": "none",
                }
            )
            return {
                "result": self._build_failure_result(failed_state, message),
                "next_action": "end",
            }

    # 执行成功或不可修复错误直接结束，仅可定位的 SQL 错误在预算内返回生成节点。
    def _continue_after_execution(
        self,
        state: _SqlQueryState,
    ) -> Literal["generate_sql", "end"]:
        if state.get("next_action") == "generate_sql":
            return "generate_sql"
        return "end"

    # 将生成或校验阶段的状态归一为统一失败结果，保证调用方无需读取 LangGraph 内部字段。
    def _build_failure_result(
        self,
        state: _SqlQueryState,
        error: str,
    ) -> SqlQuerySubgraphResult:
        return SqlQuerySubgraphResult(
            status="failure",
            schema_results=state.get("schema_results", []),
            draft=state.get("draft"),
            error=error,
            error_code=state.get("error_code") or None,
            error_diagnostic=state.get("error_diagnostic"),
            retry_target=state.get("retry_target", "none"),
            generation_count=state.get("generation_count", 0),
            max_generation_count=state.get(
                "max_generation_count", DEFAULT_SQL_GENERATION_COUNT
            ),
            raw_model_response=_serialize_raw_responses(
                state.get("raw_model_responses", [])
            ),
        )

    # 运行独立子图并校验生成预算；Pipeline 可复用规划结构，缺项由共享缓存按需补齐。
    def run(
        self,
        query_plan: NaturalLanguageQueryPlan,
        schema_results: list[TableSchemaToolResponse] | None = None,
        max_generation_count: int = DEFAULT_SQL_GENERATION_COUNT,
    ) -> SqlQuerySubgraphResult:
        if max_generation_count < 1:
            raise ValueError("max_generation_count 必须大于或等于 1")
        state = self._workflow.invoke(
            {
                "query_plan": query_plan,
                "provided_schema_results": schema_results,
                "generation_count": 0,
                "max_generation_count": max_generation_count,
                "raw_model_responses": [],
            }
        )
        if "result" in state:
            return state["result"]
        return self._build_failure_result(
            state,
            state.get("error", "SQL 子图未产生执行结果"),
        )
