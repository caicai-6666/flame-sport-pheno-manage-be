"""定义 SQL 生成、校验与只读执行子图的状态和节点。"""

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, TypedDict

import asyncmy
from asyncmy.cursors import DictCursor
from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)
from sqlglot import exp, parse, parse_one
from sqlglot.errors import ParseError

from app.agent.text2sql.domains.base import QueryDomainProfile
from app.agent.text2sql.events.models import AgentProgressUpdate
from app.agent.text2sql.events.publisher import AgentProgressReporter, ProgressEmitter
from app.agent.text2sql.model_messages import (
    ModelMessageTraceQueue,
    create_traced_chat_completion,
)
from app.core.config import Settings, get_settings
from app.agent.text2sql.shared.model_options import (
    DEFAULT_SQL_MAX_TOKENS,
    get_model_request_profile,
    resolve_model_provider_connection,
)
from app.agent.text2sql.shared.tool_tag_template import (
    build_tool_tag_prefixed_task_content,
    load_tool_tag_template,
    resolve_query_tool_tag_template_filename,
)
from app.agent.text2sql.subgraphs.planning.tools.query_plan import (
    NaturalLanguageQueryPlan,
    QueryPlanBlock,
    QueryPlanJoin,
    QueryPlanQuantifiedCondition,
)
from app.agent.text2sql.subgraphs.planning.tools.table_schema_cache import CachingTableSchemaReader
from app.agent.text2sql.subgraphs.planning.tools.table_schema_reader import InformationSchemaTableSchemaReader
from app.agent.text2sql.shared.yaml_context import parse_yaml_context, render_yaml_context
from app.agent.text2sql.subgraphs.planning.tools.table_schema import TableSchemaToolResponse
from app.agent.text2sql.function_calling.feedback import (
    build_tool_argument_error_message,
)
from app.agent.text2sql.subgraphs.sql.tool import (
    SUBMIT_SQL_QUERY_TOOL_NAME,
    SqlQueryDraft,
    SqlQueryParameter,
    SqlScalar,
    build_sql_execution_error_message,
    build_sql_protocol_retry_message,
    build_sql_query_tool_definition,
    build_sql_validation_error_message,
    parse_sql_query_tool_arguments,
)
from app.agent.text2sql.subgraphs.sql.models import MaterialSqlQueryPlan


QUERY_EXECUTION_TIMEOUT_SECONDS: Final[float] = 10.0
DEFAULT_SQL_GENERATION_COUNT: Final[int] = 2
TraceWriter = Callable[[str], None]
SchemaReader = Callable[[str], TableSchemaToolResponse]
SqlExecutor = Callable[
    [str, tuple[SqlScalar, ...]],
    list[dict[str, Any]] | Awaitable[list[dict[str, Any]]],
]
SqlInputPlan = NaturalLanguageQueryPlan | MaterialSqlQueryPlan
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

    query_plan: SqlInputPlan
    provided_schema_results: list[TableSchemaToolResponse] | None
    schema_results: list[TableSchemaToolResponse]
    messages: list[Any]
    draft: SqlQueryDraft
    raw_model_responses: list[str]
    generation_count: int
    max_generation_count: int
    current_tool_call_id: str
    current_turn_start: int
    repair_context_start: int | None
    repair_target: Literal[
        "tool_protocol",
        "tool_arguments",
        "sql_validation",
        "sql_execution",
    ] | None
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


# 兼容同步测试替身和正式异步依赖，确保生产路径不会退回阻塞式事件循环包装。
async def _resolve_maybe_awaitable(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# 修正成功后删除此前失败 SQL 调用及反馈，并返回当前有效调用在压缩上下文中的新位置。
def _clear_repaired_sql_context(
    messages: list[Any],
    repair_context_start: int | None,
    current_turn_start: int,
) -> int:
    if repair_context_start is None:
        return current_turn_start
    del messages[repair_context_start:current_turn_start]
    return repair_context_start


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
9. 对 all 和 none 量词，成员 predicate 不能先作为外层普通 WHERE 删除反例。必须按 quantified_conditions 由 quantifier 唯一派生的实现完成主体资格判定；any 使用 EXISTS，all/none 使用 NOT EXISTS，exactly/at_least/at_most 使用规范 HAVING。每个 EXISTS/NOT EXISTS 必须写成规范的 `SELECT 1`，FROM 精确使用 collection_base_source，按顺序完整实现 collection_joins，且不得附加 DISTINCT、GROUP BY、HAVING、ORDER BY、LIMIT、OFFSET 或集合运算；同一子查询还要完整实现 correlation_condition、全部 collection_filters 和正确方向的成员 predicate/反条件。require_non_empty=true 时还必须按相同内部关系用正向 EXISTS 证明集合非空。数量型量词必须在所属块 HAVING 中精确写为 `COUNT(DISTINCT CASE WHEN collection_filters AND predicate THEN member_key END) <operator> :count`，exactly/at_least/at_most 的 operator 分别是 `=`/`>=`/`<=`。若最终还要返回集合成员行，应先确定合格主体，再在外层关联并返回成员。
10. query_blocks 是不可跨越的条件作用域。每个非根查询块必须实现为与 block_id 同名的一个 CTE，且只能读取自身 source_tables 和 input_blocks；根查询块是最外层 SELECT。不得遗漏、增加或重命名计划查询块，也不得把某块的 JOIN、WHERE、GROUP BY、HAVING 或量词条件移动到其他块。
11. 每个查询块的 SELECT 必须按该块 select_fields 顺序显式使用 AS result_field。后续块只能通过 `input_block.result_field` 引用前置块输出。最终 result_columns 只填写根查询块的 result_field。
12. aliases 对查询块内的外层 SELECT 和嵌套子查询共同生效。必须精确使用计划已声明的角色别名，未声明 aliases 时不得自行为表或 CTE 添加别名。不得为 EXISTS/NOT EXISTS 成员表临时发明 `alias2`、`member_alias` 等新别名，也不得改回真实表名；量词成员表只在相关子查询使用时，不要把它额外加入外层 FROM 或 JOIN。
13. 每个查询块的外层关系形状必须逐字段实现：FROM 起始数据源精确等于 base_source；JOIN 按计划顺序精确使用 right_source 和 join_type；完整 condition 只能位于对应 JOIN ON，不得多加 ON 条件、换成逗号连接或移入 WHERE。deduplication.mode=distinct 时必须 SELECT DISTINCT，mode=none 时禁止 DISTINCT。cardinality 和 right_key 只是规划层已确认的行放大语义，SQL 不得据此自行改写聚合或去重。

输入是合法 YAML，字段含义固定如下：
- `query_plan` 是 SQL 层唯一权威的数据获取计划；`query_goal` 是查询目标，`root_block_id` 标识最外层结果块，`query_blocks` 按依赖拓扑顺序排列。
- `tables` 列出参与表；每项的 `table_name` 是真实表名，`role` 是本次职责。
- 每个查询块的 `row_granularity` 和 `grain_fields` 定义本块行粒度；`source_tables` 是本块及其普通子查询读取的真实表，`input_blocks` 是本块直接读取的前置 CTE。`base_source` 是外层 FROM；`joins` 依次定义右侧数据源、INNER/LEFT 保留语义、关联基数、右侧键和完整 ON；`deduplication` 唯一定义 SELECT DISTINCT。`aliases`、`filters`、`quantified_conditions`、`group_by`、`aggregations`、`having` 和 `order_by` 只在所属块生效。
- `quantified_conditions.subject_key` 是资格主体键，数量型量词的单一 `member_key` 是去重计数键；`predicate`、`correlation_condition`、`collection_filters` 不能互相替代。all/any/none 的 `collection_base_source` 与 `collection_joins` 唯一定义相关子查询内部关系形状；数量型量词固定将这两项设为 null 和空列表。SQL 实现由 quantifier 唯一派生，`require_non_empty` 则唯一决定 all/none 是否额外要求集合非空。
- `implemented_business_rules` 只把核心规则映射到 joins、filters、quantified_conditions 等已有计划组件，便于审计覆盖情况；不得把它当成第二份 SQL 表达式来源。SQL 只需准确实现被引用的正式计划组件。
- `strategy` 和 `strategy_reason` 只是规划层由正式组件派生的人工审阅信息；不得据此改变 base_source、joins、quantified_conditions、分组或筛选。
- `select_fields` 是所属查询块的输出字段；`field` 是字段或表达式，`result_field` 是该块 SQL 必须输出的 AS 别名，`purpose` 是简短表头标签。
- `pagination.limit` 是根查询块最大结果行数或 null，`pagination.offset` 是根查询块偏移量。
- `business_caliber` 只用于解释及审计；每项 `plan_references` 已指向真正执行该口径的正式计划组件，不得根据 description 额外增删 SQL 条件。`assumptions` 必须为空列表。
- `allowed_table_schemas` 是允许读取的表结构列表；每项的 `table` 是真实表名，`columns` 是字段列表，`field_name`、`data_type`、`foreign_key`、`comment` 分别表示真实字段、数据库类型、外键目标和字段注释。

只能按以上固定含义读取 YAML，不能把 YAML 键名误当成数据库标识符。"""


# 将已确认的查询计划和按需读取的结构作为 SQL 生成模型的唯一动态上下文。
def _build_sql_generation_messages(
    query_plan: NaturalLanguageQueryPlan,
    schema_results: list[TableSchemaToolResponse],
    tool_tag_template: str | None = None,
) -> list[dict[str, str]]:
    context = {
        "query_plan": query_plan.model_dump(),
        "allowed_table_schemas": [
            parse_yaml_context(item.result) for item in schema_results
        ],
    }
    task_content = render_yaml_context(context)
    task_content = build_tool_tag_prefixed_task_content(
        task_content,
        tool_tag_template,
        (
            "必须调用 submit_sql_query，不要输出普通文本。"
            "请严格按照以下 tool-tag 结构输出真实工具名和合法 JSON 参数。"
        ),
        "SQL 生成任务",
    )
    return [
        {
            "role": "system",
            "content": _build_sql_generation_system_prompt(),
        },
        {
            "role": "user",
            "content": task_content,
        },
    ]


# 校验计划中的表名属于当前业务域白名单，并在保持计划顺序的同时去重。
def _get_plan_table_names(
    query_plan: SqlInputPlan,
    allowed_table_names: frozenset[str],
) -> list[str]:
    if isinstance(query_plan, MaterialSqlQueryPlan):
        declared_table_names = query_plan.required_tables
    else:
        declared_table_names = [table.table_name for table in query_plan.tables]
    table_names: list[str] = []
    seen_table_names: set[str] = set()
    for table_name in declared_table_names:
        if table_name not in allowed_table_names:
            raise SqlValidationError(
                code="query_plan_table_forbidden",
                message=f"查询计划包含不允许读取的表：{table_name}",
                repair_action=(
                    f"从查询计划中删除表 `{table_name}` 及所有引用该表的"
                    "关联、筛选、返回、分组、聚合和排序表达式。"
                ),
                retry_target="query_planning",
                details={"table_name": table_name},
            )
        if table_name not in seen_table_names:
            seen_table_names.add(table_name)
            table_names.append(table_name)
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
    locking_selects = [
        select_expression
        for select_expression in expression.find_all(exp.Select)
        if select_expression.args.get("locks")
    ]
    if expression.args.get("locks") or locking_selects:
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
        unsupported_table_modifiers = sorted(
            key
            for key, value in table.args.items()
            if key not in {"this", "db", "catalog", "alias"}
            and value is not None
            and value is not False
            and value != []
        )
        if unsupported_table_modifiers:
            raise SqlValidationError(
                code="table_modifier_forbidden",
                message=(
                    f"SQL 数据源 {table.name} 使用了计划未声明的表修饰："
                    + "、".join(unsupported_table_modifiers)
                ),
                repair_action=(
                    f"删除表 `{table.name}` 上的 PARTITION、TABLESAMPLE、版本或其他"
                    "数据范围修饰，只读取计划声明的完整数据源。"
                ),
                details={
                    "table_name": table.name,
                    "modifiers": unsupported_table_modifiers,
                },
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
    if (
        isinstance(literal.parent, exp.If)
        and literal.arg_key == "true"
    ) or (
        isinstance(literal.parent, exp.Case)
        and literal.arg_key == "default"
    ):
        return False
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
    alias_to_table_name = (
        {
            table.alias: table.name
            for table in expression.find_all(exp.Table)
            if table.alias and table.alias != table.name
        }
        if declared_aliases is None
        else dict(declared_aliases)
    )

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


# 移除 AST 中不改变运算结构的显式括号，避免同义条件因排版不同被误拒。
def _remove_expression_parentheses(expression: exp.Expression) -> exp.Expression:
    # 用内部子树替换 Paren 节点，sqlglot 会按实际优先级重新渲染必要括号。
    def replace_parenthesis(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Paren):
            return node.this.copy()
        return node

    return expression.copy().transform(replace_parenthesis)


# 使用归一化 MySQL 文本比较两个表达式子树，忽略排版和等值数值精度差异。
def _expressions_are_equivalent(
    left: exp.Expression,
    right: exp.Expression,
) -> bool:
    normalized_left = _normalize_numeric_literals(
        _remove_expression_parentheses(left)
    )
    normalized_right = _normalize_numeric_literals(
        _remove_expression_parentheses(right)
    )
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
            cte_alias = cte.args.get("alias")
            cte_column_aliases = (
                list(cte_alias.args.get("columns") or [])
                if isinstance(cte_alias, exp.TableAlias)
                else []
            )
            cte_modifiers = sorted(
                key
                for key, value in cte.args.items()
                if key not in {"this", "alias"}
                and value is not None
                and value is not False
                and value != []
            )
            if cte_column_aliases or cte_modifiers:
                raise SqlValidationError(
                    code="query_block_cte_modifier_forbidden",
                    message=(
                        f"查询块 CTE {cte_name} 使用了计划未声明的列重命名或修饰"
                    ),
                    repair_action=(
                        f"将 CTE `{cte_name}` 改为普通 `AS (SELECT ...)`；"
                        "删除 CTE 名称后的列别名列表及 MATERIALIZED 等修饰，"
                        "输出名只能由该查询块 select_fields.result_field 决定。"
                    ),
                    details={
                        "block_id": cte_name,
                        "column_alias_count": len(cte_column_aliases),
                        "modifiers": cte_modifiers,
                    },
                )
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
        cte_select = cte.this
        if not isinstance(cte_select, exp.Select):
            raise SqlValidationError(
                code="query_block_set_operation_forbidden",
                message=(
                    f"查询块 {block_id} 必须是一条独立 SELECT，不能使用 UNION、"
                    "INTERSECT 或 EXCEPT"
                ),
                repair_action=(
                    f"将 CTE `{block_id}` 精确改为查询计划对应的一条 SELECT；"
                    "删除所有集合运算分支。"
                ),
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


# 限定每个计划查询块可出现的 SELECT 子句，拒绝协议未表达的 QUALIFY 等隐式筛选。
def _validate_query_block_select_arguments(
    select_expression: exp.Select,
    block_id: str,
    *,
    is_root: bool,
) -> None:
    allowed_arguments = {
        "expressions",
        "from_",
        "joins",
        "where",
        "group",
        "having",
        "order",
        "distinct",
    }
    if is_root:
        allowed_arguments.update({"with", "with_", "limit", "offset"})
    unexpected_arguments = sorted(
        key
        for key, value in select_expression.args.items()
        if key not in allowed_arguments
        and value is not None
        and value is not False
        and value != []
    )
    if unexpected_arguments:
        raise SqlValidationError(
            code="query_block_select_clause_forbidden",
            message=(
                f"查询块 {block_id} 使用了计划协议未声明的 SELECT 子句："
                + "、".join(unexpected_arguments)
            ),
            repair_action=(
                f"删除查询块 `{block_id}` 中的 QUALIFY、WINDOW、CONNECT、"
                "MATCH、PREWHERE 或其他未进入查询计划的附加子句；"
                "仅保留正式计划对应的 SELECT/FROM/JOIN/WHERE/GROUP BY/"
                "HAVING/ORDER BY/DISTINCT，且分页只允许位于根查询块。"
            ),
            details={
                "block_id": block_id,
                "unexpected_select_arguments": unexpected_arguments,
            },
        )


# 将 JOIN ON 顶层的 AND 拆成无序原子条件，允许等值两侧和 AND 书写顺序不同。
def _flatten_conjunction(expression: exp.Expression) -> list[exp.Expression]:
    if isinstance(expression, exp.Paren):
        return _flatten_conjunction(expression.this)
    if isinstance(expression, exp.And):
        return _flatten_conjunction(expression.this) + _flatten_conjunction(
            expression.expression
        )
    return [expression]


# 比较两个完整 JOIN ON 条件，拒绝缺失或额外的关联谓词。
def _join_conditions_are_equivalent(
    actual_condition: exp.Expression,
    expected_condition: exp.Expression,
    comparison_aliases: dict[str, str],
) -> bool:
    actual_parts = [
        _normalize_column_table_aliases(part, comparison_aliases)
        for part in _flatten_conjunction(actual_condition)
    ]
    expected_parts = _flatten_conjunction(expected_condition)
    if len(actual_parts) != len(expected_parts):
        return False
    unmatched_actual_parts = list(actual_parts)
    for expected_part in expected_parts:
        matched_index = next(
            (
                index
                for index, actual_part in enumerate(unmatched_actual_parts)
                if _expressions_are_equivalent(actual_part, expected_part)
            ),
            None,
        )
        if matched_index is None:
            return False
        unmatched_actual_parts.pop(matched_index)
    return not unmatched_actual_parts


# 将 sqlglot JOIN 归一为计划协议允许的 inner/left，其他保留语义一律拒绝。
def _get_supported_join_type(join_expression: exp.Join) -> str | None:
    side = str(join_expression.args.get("side") or "").upper()
    kind = str(join_expression.args.get("kind") or "").upper()
    if join_expression.args.get("on") is None:
        return None
    if not side and kind in {"", "INNER"}:
        return "inner"
    if side == "LEFT" and kind in {"", "OUTER"}:
        return "left"
    return None


# 精确核对查询块外层 FROM、JOIN 保留侧、ON 和 DISTINCT，阻止 SQL 渲染器二次决策。
def _validate_query_block_relation_shape(
    block_select: exp.Select,
    block: QueryPlanBlock,
    comparison_aliases: dict[str, str],
) -> None:
    from_expression = block_select.args.get("from_")
    base_table = from_expression.this if from_expression is not None else None
    if not isinstance(base_table, exp.Table):
        raise SqlValidationError(
            code="query_block_base_source_mismatch",
            message=f"SQL 查询块 {block.block_id} 没有使用计划指定的 FROM 起始数据源",
            repair_action=(
                f"将查询块 `{block.block_id}` 的外层 FROM 精确改为 "
                f"`{block.base_source}`，不得使用派生表或额外别名。"
            ),
            details={"block_id": block.block_id, "expected_base_source": block.base_source},
        )
    actual_base_source = base_table.alias_or_name
    if actual_base_source != block.base_source:
        raise SqlValidationError(
            code="query_block_base_source_mismatch",
            message=f"SQL 查询块 {block.block_id} 的 FROM 起始数据源与计划不一致",
            repair_action=(
                f"将查询块 `{block.block_id}` 的外层 FROM 起始数据源"
                f"精确改为 `{block.base_source}`；当前为 `{actual_base_source}`。"
            ),
            details={
                "block_id": block.block_id,
                "expected_base_source": block.base_source,
                "actual_base_source": actual_base_source,
            },
        )

    actual_joins = list(block_select.args.get("joins") or [])
    if len(actual_joins) != len(block.joins):
        raise SqlValidationError(
            code="query_block_join_count_mismatch",
            message=f"SQL 查询块 {block.block_id} 的外层 JOIN 数量与计划不一致",
            repair_action=(
                f"按 query_blocks[{block.block_id}].joins 的顺序且只生成 "
                f"{len(block.joins)} 个外层 JOIN；当前为 {len(actual_joins)} 个。"
            ),
            details={
                "block_id": block.block_id,
                "expected_join_count": len(block.joins),
                "actual_join_count": len(actual_joins),
            },
        )
    for join_index, (actual_join, planned_join) in enumerate(
        zip(actual_joins, block.joins, strict=True)
    ):
        actual_right_table = actual_join.this
        actual_right_source = (
            actual_right_table.alias_or_name
            if isinstance(actual_right_table, exp.Table)
            else ""
        )
        if actual_right_source != planned_join.right_source:
            raise SqlValidationError(
                code="query_block_join_source_mismatch",
                message=(
                    f"SQL 查询块 {block.block_id} 的第 {join_index + 1} 个 "
                    "JOIN 右侧数据源与计划不一致"
                ),
                repair_action=(
                    f"将 query_blocks[{block.block_id}].joins[{join_index}] "
                    f"对应的 JOIN 右侧精确改为 `{planned_join.right_source}`，"
                    f"不得改变 JOIN 顺序或添加别名。"
                ),
                details={
                    "block_id": block.block_id,
                    "join_index": join_index,
                    "expected_right_source": planned_join.right_source,
                    "actual_right_source": actual_right_source or None,
                },
            )
        actual_join_type = _get_supported_join_type(actual_join)
        if actual_join_type != planned_join.join_type:
            raise SqlValidationError(
                code="query_block_join_type_mismatch",
                message=(
                    f"SQL 查询块 {block.block_id} 的第 {join_index + 1} 个 "
                    "JOIN 保留语义与计划不一致"
                ),
                repair_action=(
                    f"将 query_blocks[{block.block_id}].joins[{join_index}] "
                    f"对应的 JOIN 类型精确改为 `{planned_join.join_type}`，"
                    "不得换成逗号关联、CROSS/RIGHT/FULL JOIN 或移除 ON。"
                ),
                details={
                    "block_id": block.block_id,
                    "join_index": join_index,
                    "expected_join_type": planned_join.join_type,
                    "actual_join_type": actual_join_type,
                },
            )
        actual_on = actual_join.args.get("on")
        expected_on = _parse_planned_join_condition(
            planned_join.condition,
            join_index,
        )
        if actual_on is None or not _join_conditions_are_equivalent(
            actual_on,
            expected_on,
            comparison_aliases,
        ):
            raise SqlValidationError(
                code="query_block_join_condition_mismatch",
                message=(
                    f"SQL 查询块 {block.block_id} 的第 {join_index + 1} 个 "
                    "JOIN ON 与计划完整条件不一致"
                ),
                repair_action=(
                    f"将 query_blocks[{block.block_id}].joins[{join_index}] "
                    f"对应的 ON 精确改为 `{planned_join.condition}`；"
                    "不得缺少、多加条件或移入 WHERE/HAVING。"
                ),
                details={
                    "block_id": block.block_id,
                    "join_index": join_index,
                    "expected_condition": planned_join.condition,
                },
            )

    distinct_expression = block_select.args.get("distinct")
    actual_distinct = bool(distinct_expression)
    expected_distinct = block.deduplication.mode == "distinct"
    has_distinct_modifiers = (
        isinstance(distinct_expression, exp.Distinct)
        and any(
            value is not None and value is not False and value != []
            for value in distinct_expression.args.values()
        )
    )
    if actual_distinct != expected_distinct or has_distinct_modifiers:
        expected_sql = "SELECT DISTINCT" if expected_distinct else "SELECT"
        raise SqlValidationError(
            code="query_block_deduplication_mismatch",
            message=f"SQL 查询块 {block.block_id} 的 DISTINCT 语义与计划不一致",
            repair_action=(
                f"将查询块 `{block.block_id}` 的投影精确改为 `{expected_sql}`；"
                f"该块 deduplication.mode 是 `{block.deduplication.mode}`。"
            ),
            details={
                "block_id": block.block_id,
                "expected_deduplication": block.deduplication.mode,
                "actual_distinct": actual_distinct,
                "has_distinct_modifiers": has_distinct_modifiers,
            },
        )


# 识别 WHERE 顶层的 EXISTS 极性，供数量与量词契约精确对齐。
def _get_exists_clause_polarity(
    expression: exp.Expression,
) -> Literal["exists", "not_exists"] | None:
    normalized_expression = _remove_expression_parentheses(expression)
    if isinstance(normalized_expression, exp.Exists):
        return "exists"
    if isinstance(normalized_expression, exp.Not) and isinstance(
        normalized_expression.this,
        exp.Exists,
    ):
        return "not_exists"
    return None


# 将已声明普通条件与实际子句原子谓词一对一匹配，返回未被消费的实际条件。
def _remove_matching_clause_conditions(
    actual_parts: list[exp.Expression],
    expected_parts: list[exp.Expression],
    comparison_aliases: dict[str, str],
) -> tuple[list[exp.Expression], list[exp.Expression]]:
    unmatched_actual_parts = [
        _normalize_column_table_aliases(part, comparison_aliases)
        for part in actual_parts
    ]
    missing_expected_parts: list[exp.Expression] = []
    for expected_part in expected_parts:
        matched_index = next(
            (
                index
                for index, actual_part in enumerate(unmatched_actual_parts)
                if _expressions_are_equivalent(actual_part, expected_part)
            ),
            None,
        )
        if matched_index is None:
            missing_expected_parts.append(expected_part)
            continue
        unmatched_actual_parts.pop(matched_index)
    return unmatched_actual_parts, missing_expected_parts


# 精确校验查询块 WHERE/HAVING 的普通条件和量词占位，拒绝模型自行加码。
def _validate_query_block_clause_shape(
    block_select: exp.Select,
    block: QueryPlanBlock,
    comparison_aliases: dict[str, str],
) -> None:
    where_clause = block_select.args.get("where")
    actual_where_parts = (
        _flatten_conjunction(where_clause.this)
        if where_clause is not None and where_clause.this is not None
        else []
    )
    expected_filter_parts = [
        part
        for filter_index, query_filter in enumerate(block.filters)
        for part in _flatten_conjunction(
            _parse_query_block_condition(
                query_filter.condition,
                block.block_id,
                "filters",
                filter_index,
            )
        )
    ]
    remaining_where_parts, missing_filter_parts = _remove_matching_clause_conditions(
        actual_where_parts,
        expected_filter_parts,
        comparison_aliases,
    )
    expected_exists_count = sum(
        condition.implementation == "exists"
        for condition in block.quantified_conditions
    ) + sum(
        condition.require_non_empty
        for condition in block.quantified_conditions
    )
    expected_not_exists_count = sum(
        condition.implementation == "not_exists"
        for condition in block.quantified_conditions
    )
    actual_exists_polarities = [
        _get_exists_clause_polarity(part)
        for part in remaining_where_parts
    ]
    where_shape_matches = (
        not missing_filter_parts
        and all(polarity is not None for polarity in actual_exists_polarities)
        and actual_exists_polarities.count("exists") == expected_exists_count
        and actual_exists_polarities.count("not_exists")
        == expected_not_exists_count
    )
    if not where_shape_matches:
        raise SqlValidationError(
            code="query_block_where_shape_mismatch",
            message=(
                f"SQL 查询块 {block.block_id} 的 WHERE 条件集合与正式计划不一致"
            ),
            repair_action=(
                f"在查询块 `{block.block_id}` 中只保留 filters 声明的"
                f" {len(expected_filter_parts)} 个原子条件、{expected_exists_count} 个 "
                f"EXISTS 和 {expected_not_exists_count} 个 NOT EXISTS；"
                "删除未声明的 WHERE 条件，不得把计划条件移入子查询。"
            ),
            details={
                "block_id": block.block_id,
                "expected_filter_atom_count": len(expected_filter_parts),
                "missing_filter_atom_count": len(missing_filter_parts),
                "expected_exists_count": expected_exists_count,
                "expected_not_exists_count": expected_not_exists_count,
                "actual_remaining_polarities": actual_exists_polarities,
            },
        )

    if any(
        condition.implementation == "having"
        for condition in block.quantified_conditions
    ):
        return
    having_clause = block_select.args.get("having")
    actual_having_parts = (
        _flatten_conjunction(having_clause.this)
        if having_clause is not None and having_clause.this is not None
        else []
    )
    expected_having_parts = [
        part
        for having_index, having_condition in enumerate(block.having)
        for part in _flatten_conjunction(
            _parse_query_block_condition(
                having_condition.condition,
                block.block_id,
                "having",
                having_index,
            )
        )
    ]
    remaining_having_parts, missing_having_parts = (
        _remove_matching_clause_conditions(
            actual_having_parts,
            expected_having_parts,
            comparison_aliases,
        )
    )
    if missing_having_parts or remaining_having_parts:
        raise SqlValidationError(
            code="query_block_having_shape_mismatch",
            message=(
                f"SQL 查询块 {block.block_id} 的 HAVING 条件集合与正式计划不一致"
            ),
            repair_action=(
                f"将查询块 `{block.block_id}` 的 HAVING 精确改为 having "
                f"声明的 {len(expected_having_parts)} 个原子条件；"
                "删除未声明条件，不得把 WHERE 或量词条件搬入 HAVING。"
            ),
            details={
                "block_id": block.block_id,
                "expected_having_atom_count": len(expected_having_parts),
                "missing_having_atom_count": len(missing_having_parts),
                "extra_having_atom_count": len(remaining_having_parts),
            },
        )


# 校验查询块输出别名、数据源和关联条件均落实在其自身 AST 作用域。
def _validate_query_block_implementation(
    expression: exp.Expression,
    query_plan: NaturalLanguageQueryPlan,
) -> dict[str, exp.Select]:
    block_selects = _get_query_block_selects(expression, query_plan)
    all_block_ids = set(block_selects)
    for block in query_plan.query_blocks:
        block_select = block_selects[block.block_id]
        if block.block_id != query_plan.root_block_id and (
            block_select.args.get("limit") is not None
            or block_select.args.get("offset") is not None
        ):
            raise SqlValidationError(
                code="query_block_pagination_forbidden",
                message=f"非根查询块 {block.block_id} 不允许自行截断中间结果",
                repair_action=(
                    f"删除 CTE `{block.block_id}` 内的 LIMIT 和 OFFSET；"
                    "pagination 只允许由根查询块实现。"
                ),
                details={"block_id": block.block_id},
            )
        _validate_query_block_select_arguments(
            block_select,
            block.block_id,
            is_root=block.block_id == query_plan.root_block_id,
        )
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
            expected_alias_sources = {
                alias.alias: alias.source_table for alias in block.aliases
            }
            actual_alias_sources: dict[str, set[str]] = {}
            unexpected_unaliased_sources: set[str] = set()
            aliased_source_tables = set(expected_alias_sources.values())
            for table in block_select.find_all(exp.Table):
                if _find_node_query_block(table, block_selects) != block.block_id:
                    continue
                if table.name in all_block_ids:
                    continue
                if table.alias:
                    actual_alias_sources.setdefault(table.alias, set()).add(table.name)
                elif table.name in aliased_source_tables:
                    unexpected_unaliased_sources.add(table.name)
            actual_role_aliases = {
                table.alias_or_name
                for table in block_select.find_all(exp.Table)
                if _find_node_query_block(table, block_selects) == block.block_id
                and table.name not in all_block_ids
            }
            expected_role_aliases = {alias.alias for alias in block.aliases} | (
                set(block.source_tables) - aliased_source_tables
            )
            alias_source_mismatches = {
                alias_name: sorted(actual_sources)
                for alias_name, actual_sources in actual_alias_sources.items()
                if alias_name not in expected_alias_sources
                or actual_sources != {expected_alias_sources[alias_name]}
            }
            missing_declared_aliases = sorted(
                set(expected_alias_sources) - set(actual_alias_sources)
            )
            if (
                actual_role_aliases != expected_role_aliases
                or alias_source_mismatches
                or missing_declared_aliases
                or unexpected_unaliased_sources
            ):
                raise SqlValidationError(
                    code="query_block_alias_mismatch",
                    message=(
                        f"SQL 查询块 {block.block_id} 的角色别名或其真实来源表与计划不一致"
                    ),
                    repair_action=(
                        f"查询块 `{block.block_id}` 必须精确使用别名到真实表的映射 "
                        f"{expected_alias_sources}；不得交换别名来源、遗漏角色或把已声明"
                        "别名的表改回无别名引用。"
                    ),
                    details={
                        "block_id": block.block_id,
                        "expected_aliases": sorted(expected_role_aliases),
                        "actual_aliases": sorted(actual_role_aliases),
                        "expected_alias_sources": expected_alias_sources,
                        "alias_source_mismatches": alias_source_mismatches,
                        "missing_declared_aliases": missing_declared_aliases,
                        "unexpected_unaliased_sources": sorted(
                            unexpected_unaliased_sources
                        ),
                    },
                )
        else:
            undeclared_aliases = sorted(
                {
                    table.alias
                    for table in block_select.find_all(exp.Table)
                    if _find_node_query_block(table, block_selects) == block.block_id
                    and table.alias
                    and table.alias != table.name
                }
            )
            if undeclared_aliases:
                raise SqlValidationError(
                    code="query_block_alias_mismatch",
                    message=(
                        f"SQL 查询块 {block.block_id} 使用了计划未声明的别名"
                    ),
                    repair_action=(
                        f"查询块 `{block.block_id}` 未声明 aliases，请删除别名 "
                        f"{undeclared_aliases}；如同一真实表必须承担多个角色，"
                        "则返回查询规划阶段显式声明所有角色别名。"
                    ),
                    details={
                        "block_id": block.block_id,
                        "undeclared_aliases": undeclared_aliases,
                    },
                )
        _validate_query_block_relation_shape(
            block_select,
            block,
            comparison_aliases,
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
        actual_group_modifiers = (
            sorted(
                key
                for key, value in actual_group.args.items()
                if key != "expressions"
                and value is not None
                and value is not False
                and value != []
            )
            if actual_group is not None
            else []
        )
        if actual_group_modifiers:
            raise SqlValidationError(
                code="query_block_group_modifier_forbidden",
                message=(
                    f"查询块 {block.block_id} 的 GROUP BY 使用了计划未声明的修饰："
                    + "、".join(actual_group_modifiers)
                ),
                repair_action=(
                    f"删除查询块 `{block.block_id}` 的 WITH ROLLUP、CUBE、"
                    "GROUPING SETS 或其他分组修饰，只保留计划中的 group_by 字段。"
                ),
                details={
                    "block_id": block.block_id,
                    "modifiers": actual_group_modifiers,
                },
            )
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
                expected_nulls_first = actual_direction == "ASC"
                actual_nulls_first = (
                    bool(actual_order_item.args.get("nulls_first"))
                    if isinstance(actual_order_item, exp.Ordered)
                    else expected_nulls_first
                )
                has_order_modifier = (
                    isinstance(actual_order_item, exp.Ordered)
                    and actual_order_item.args.get("with_fill") is not None
                )
                if not _expressions_are_equivalent(
                    _normalize_column_table_aliases(
                        actual_order_field,
                        comparison_aliases,
                    ),
                    expected_order_field,
                ) or (
                    actual_direction != planned_order_item.direction
                    or actual_nulls_first != expected_nulls_first
                    or has_order_modifier
                ):
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
        _validate_query_block_clause_shape(
            block_select,
            block,
            comparison_aliases,
        )
    return block_selects


# 校验相关子查询中的主体关联确实跨越内外层作用域，拒绝只关联两个内层表的伪相关条件。
def _exists_contains_outer_correlation(
    exists_expression: exp.Exists,
    expected_correlation: exp.Expression,
    sql_aliases: dict[str, str],
) -> bool:
    local_select = exists_expression.this
    if not isinstance(local_select, exp.Select):
        return False
    local_aliases = {
        table.alias_or_name
        for table in local_select.find_all(exp.Table)
        if table.find_ancestor(exp.Select) is local_select
    }
    actual_equalities = list(local_select.find_all(exp.EQ))
    for expected_part in _flatten_conjunction(expected_correlation):
        if not isinstance(expected_part, exp.EQ):
            return False
        matching_candidate = next(
            (
                candidate
                for candidate in actual_equalities
                if _expressions_are_equivalent(
                    _normalize_column_table_aliases(candidate, sql_aliases),
                    expected_part,
                )
            ),
            None,
        )
        if matching_candidate is None:
            return False
        candidate_columns = list(matching_candidate.find_all(exp.Column))
        if len(candidate_columns) != 2:
            return False
        local_flags = [
            bool(column.table) and column.table in local_aliases
            for column in candidate_columns
        ]
        if not any(local_flags) or all(local_flags):
            return False
    return True


# 只返回当前 SELECT 顶层 WHERE 合取项中的 EXISTS，防止嵌套子查询冒充量词实现。
def _get_top_level_where_exists(
    select_expression: exp.Select,
) -> list[tuple[exp.Exists, bool]]:
    where_expression = select_expression.args.get("where")
    if where_expression is None or where_expression.this is None:
        return []
    exists_items: list[tuple[exp.Exists, bool]] = []
    for condition_part in _flatten_conjunction(where_expression.this):
        current_part = condition_part
        while isinstance(current_part, exp.Paren):
            current_part = current_part.this
        if isinstance(current_part, exp.Exists):
            exists_items.append((current_part, False))
        elif isinstance(current_part, exp.Not) and isinstance(
            current_part.this,
            exp.Exists,
        ):
            exists_items.append((current_part.this, True))
    return exists_items


# 精确校验量词子查询的 SELECT、FROM 和有序 JOIN，禁止内部再次改变集合范围。
def _quantified_select_has_exact_relation_shape(
    select_expression: exp.Select,
    expected_base_source: str,
    expected_joins: list[QueryPlanJoin],
    comparison_aliases: dict[str, str],
) -> bool:
    allowed_select_arguments = {"expressions", "from_", "joins", "where"}
    if any(
        key not in allowed_select_arguments
        and value is not None
        and value is not False
        and value != []
        for key, value in select_expression.args.items()
    ):
        return False
    if len(select_expression.expressions) != 1:
        return False
    projection = select_expression.expressions[0]
    if not isinstance(projection, exp.Literal) or not projection.is_int:
        return False
    if int(projection.this) != 1:
        return False
    from_expression = select_expression.args.get("from_")
    base_table = from_expression.this if from_expression is not None else None
    if not isinstance(base_table, exp.Table):
        return False
    if base_table.alias_or_name != expected_base_source:
        return False
    actual_joins = list(select_expression.args.get("joins") or [])
    if len(actual_joins) != len(expected_joins):
        return False
    for join_index, (actual_join, expected_join) in enumerate(
        zip(actual_joins, expected_joins, strict=True)
    ):
        actual_right_table = actual_join.this
        if not isinstance(actual_right_table, exp.Table):
            return False
        if actual_right_table.alias_or_name != expected_join.right_source:
            return False
        if _get_supported_join_type(actual_join) != expected_join.join_type:
            return False
        actual_on = actual_join.args.get("on")
        expected_on = _parse_planned_join_condition(
            expected_join.condition,
            join_index,
        )
        if actual_on is None or not _join_conditions_are_equivalent(
            actual_on,
            expected_on,
            comparison_aliases,
        ):
            return False
    return True


# 要求成员条件、集合范围和内外层关联同时出现在同一个正确极性的 EXISTS 子查询中。
def _exists_tree_satisfies_quantified_contract(
    expression: exp.Expression,
    expected_base_source: str,
    expected_joins: list[QueryPlanJoin],
    expected_predicate: exp.Expression,
    expected_correlation: exp.Expression | None,
    expected_collection_filters: list[exp.Expression],
    require_not_exists: bool,
    block_id: str | None = None,
    block_selects: dict[str, exp.Select] | None = None,
    preserve_declared_aliases: bool = False,
    consumed_exists_ids: set[int] | None = None,
) -> bool:
    sql_aliases = (
        {} if preserve_declared_aliases else _build_sql_alias_mapping(expression)
    )
    owning_select = expression if isinstance(expression, exp.Select) else expression.find(exp.Select)
    if not isinstance(owning_select, exp.Select):
        return False
    for exists_expression, is_not_exists in _get_top_level_where_exists(
        owning_select
    ):
        if consumed_exists_ids is not None and id(exists_expression) in consumed_exists_ids:
            continue
        if is_not_exists != require_not_exists:
            continue
        if (
            block_id is not None
            and block_selects is not None
            and _find_node_query_block(exists_expression, block_selects) != block_id
        ):
            continue
        local_select = exists_expression.this
        if not isinstance(local_select, exp.Select):
            continue
        if not _quantified_select_has_exact_relation_shape(
            local_select,
            expected_base_source,
            expected_joins,
            sql_aliases,
        ):
            continue
        where_expression = local_select.args.get("where")
        if where_expression is None or where_expression.this is None:
            continue
        expected_parts = [expected_predicate] + expected_collection_filters
        if expected_correlation is not None:
            expected_parts.append(expected_correlation)
        expected_atomic_parts = [
            part
            for expected_part in expected_parts
            for part in _flatten_conjunction(expected_part)
        ]
        remaining_parts, missing_parts = _remove_matching_clause_conditions(
            _flatten_conjunction(where_expression.this),
            expected_atomic_parts,
            sql_aliases,
        )
        if missing_parts or remaining_parts:
            continue
        if expected_correlation is not None and not _exists_contains_outer_correlation(
            exists_expression,
            expected_correlation,
            sql_aliases,
        ):
            continue
        if consumed_exists_ids is not None:
            consumed_exists_ids.add(id(exists_expression))
        return True
    return False


# 根据数量量词构造唯一的条件去重计数 HAVING，供 SQL AST 精确比较。
def _build_expected_quantified_count_condition(
    condition: QueryPlanQuantifiedCondition,
    condition_index: int,
) -> exp.Expression:
    operator_by_quantifier = {
        "exactly": "=",
        "at_least": ">=",
        "at_most": "<=",
    }
    operator = operator_by_quantifier[condition.quantifier]
    assert condition.count is not None
    assert len(condition.member_key) == 1
    member_condition = " AND ".join(
        f"({condition_text})"
        for condition_text in condition.collection_filters + [condition.predicate]
    )
    condition_sql = (
        "COUNT(DISTINCT CASE WHEN "
        f"{member_condition} THEN {condition.member_key[0]} END) "
        f"{operator} {condition.count}"
    )
    try:
        select_expression = parse_one(
            f"SELECT 1 HAVING {condition_sql}",
            read="mysql",
        )
    except ParseError as error:
        raise SqlValidationError(
            code="invalid_quantified_count_contract",
            message=(
                f"查询计划中的数量量词 quantified_conditions"
                f"[{condition_index}] 无法组成规范 HAVING"
            ),
            repair_action=(
                "返回查询规划阶段，把该量词的 predicate、collection_filters "
                "和单一 member_key 改为真实字段组成的 MySQL 表达式。"
            ),
            retry_target="query_planning",
            details={"condition_index": condition_index},
        ) from error
    having_expression = select_expression.args.get("having")
    assert having_expression is not None
    return having_expression.this


# 校验 all/none 要求非空时的正向 EXISTS 只包含主体关联和集合范围。
def _exists_tree_satisfies_non_empty_contract(
    expression: exp.Expression,
    expected_base_source: str,
    expected_joins: list[QueryPlanJoin],
    expected_correlation: exp.Expression,
    expected_collection_filters: list[exp.Expression],
    block_id: str,
    block_selects: dict[str, exp.Select],
    preserve_declared_aliases: bool,
    consumed_exists_ids: set[int] | None = None,
) -> bool:
    sql_aliases = (
        {} if preserve_declared_aliases else _build_sql_alias_mapping(expression)
    )
    expected_parts = [
        part
        for expected_expression in [
            expected_correlation,
            *expected_collection_filters,
        ]
        for part in _flatten_conjunction(expected_expression)
    ]
    owning_select = expression if isinstance(expression, exp.Select) else expression.find(exp.Select)
    if not isinstance(owning_select, exp.Select):
        return False
    for exists_expression, is_not_exists in _get_top_level_where_exists(
        owning_select
    ):
        if consumed_exists_ids is not None and id(exists_expression) in consumed_exists_ids:
            continue
        if is_not_exists:
            continue
        if _find_node_query_block(exists_expression, block_selects) != block_id:
            continue
        local_select = exists_expression.this
        if not isinstance(local_select, exp.Select):
            continue
        if not _quantified_select_has_exact_relation_shape(
            local_select,
            expected_base_source,
            expected_joins,
            sql_aliases,
        ):
            continue
        where_expression = local_select.args.get("where")
        if where_expression is None or where_expression.this is None:
            continue
        actual_parts = [
            _normalize_column_table_aliases(part, sql_aliases)
            for part in _flatten_conjunction(where_expression.this)
        ]
        if len(actual_parts) != len(expected_parts):
            continue
        unmatched_parts = list(actual_parts)
        for expected_part in expected_parts:
            matched_index = next(
                (
                    index
                    for index, actual_part in enumerate(unmatched_parts)
                    if _expressions_are_equivalent(actual_part, expected_part)
                ),
                None,
            )
            if matched_index is None:
                break
            unmatched_parts.pop(matched_index)
        else:
            if not unmatched_parts and _exists_contains_outer_correlation(
                exists_expression,
                expected_correlation,
                sql_aliases,
            ):
                if consumed_exists_ids is not None:
                    consumed_exists_ids.add(id(exists_expression))
                return True
    return False


# 对数量 HAVING 和 EXISTS/NOT EXISTS 量词执行 AST 语义校验，防止运算符或空集口径变化。
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
    consumed_exists_ids_by_block: dict[str, set[int]] = {
        block.block_id: set() for block in query_plan.query_blocks
    }
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
        if condition.implementation == "having":
            numeric_conditions = [
                (candidate_index, candidate)
                for candidate_index, candidate in enumerate(
                    block.quantified_conditions
                )
                if candidate.implementation == "having"
            ]
            expected_count_conditions = [
                _build_expected_quantified_count_condition(
                    candidate,
                    candidate_index,
                )
                for candidate_index, candidate in numeric_conditions
            ]
            having_clause = block_selects[block.block_id].args.get("having")
            actual_having_parts = (
                [
                    _normalize_column_table_aliases(part, comparison_aliases)
                    for part in _flatten_conjunction(having_clause.this)
                ]
                if having_clause is not None and having_clause.this is not None
                else []
            )
            unmatched_actual_parts = list(actual_having_parts)
            all_count_conditions_match = (
                len(unmatched_actual_parts) == len(expected_count_conditions)
            )
            if all_count_conditions_match:
                for expected_count_condition in expected_count_conditions:
                    matched_index = next(
                        (
                            actual_index
                            for actual_index, actual_part in enumerate(
                                unmatched_actual_parts
                            )
                            if _expressions_are_equivalent(
                                actual_part,
                                expected_count_condition,
                            )
                        ),
                        None,
                    )
                    if matched_index is None:
                        all_count_conditions_match = False
                        break
                    unmatched_actual_parts.pop(matched_index)
            if all_count_conditions_match and not unmatched_actual_parts:
                continue
            operator_by_quantifier = {
                "exactly": "=",
                "at_least": ">=",
                "at_most": "<=",
            }
            raise SqlValidationError(
                code="quantified_count_mismatch",
                message=(
                    f"查询块 {block.block_id} 的数量量词没有精确实现计数键、"
                    "成员范围、比较符和目标数量"
                ),
                repair_action=(
                    f"将查询块 `{block.block_id}` 的 HAVING 精确改为 "
                    "`COUNT(DISTINCT CASE WHEN <collection_filters AND predicate> "
                    f"THEN {condition.member_key[0]} END) "
                    f"{operator_by_quantifier[condition.quantifier]} {condition.count}`；"
                    "不得改用其他计数键、比较符，也不得多加 HAVING 条件。"
                ),
                details={
                    "block_id": block.block_id,
                    "condition_index": index,
                    "member_key": condition.member_key[0],
                    "operator": operator_by_quantifier[condition.quantifier],
                    "count": condition.count,
                },
            )
        if condition.implementation not in {"exists", "not_exists"}:
            continue
        require_not_exists = condition.implementation == "not_exists"
        if condition.quantifier == "all" and require_not_exists:
            expected_predicate = _build_predicate_complement(expected_predicate)
        elif condition.quantifier == "none" and require_not_exists:
            expected_predicate = expected_predicate.copy()
        elif condition.quantifier == "any" and not require_not_exists:
            expected_predicate = expected_predicate.copy()
        else:
            continue
        assert condition.collection_base_source is not None
        quantified_contract_satisfied = _exists_tree_satisfies_quantified_contract(
            block_selects[block.block_id],
            condition.collection_base_source,
            condition.collection_joins,
            expected_predicate,
            expected_correlation,
            expected_collection_filters,
            require_not_exists,
            block.block_id,
            block_selects,
            bool(block.aliases),
            consumed_exists_ids_by_block[block.block_id],
        )
        non_empty_contract_satisfied = (
            not condition.require_non_empty
            or (
                expected_correlation is not None
                and _exists_tree_satisfies_non_empty_contract(
                    block_selects[block.block_id],
                    condition.collection_base_source,
                    condition.collection_joins,
                    expected_correlation,
                    expected_collection_filters,
                    block.block_id,
                    block_selects,
                    bool(block.aliases),
                    consumed_exists_ids_by_block[block.block_id],
                )
            )
        )
        if quantified_contract_satisfied and non_empty_contract_satisfied:
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
                f"在同一个 {exists_keyword} 子查询中使用 `SELECT 1 FROM "
                f"{condition.collection_base_source}`，并按 collection_joins 精确实现"
                "内部 JOIN；同时使用："
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
                "expected_collection_base_source": condition.collection_base_source,
                "expected_collection_joins": [
                    query_join.model_dump()
                    for query_join in condition.collection_joins
                ],
                "implementation": condition.implementation,
                "require_non_empty": condition.require_non_empty,
                "missing_non_empty_exists": not non_empty_contract_satisfied,
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


# 校验三字段原料计划下的最终结果列，仅约束真实 SQL 输出而不恢复旧查询块 AST。
def _validate_material_result_columns(
    expression: exp.Expression,
    result_columns: list[str],
) -> None:
    duplicate_columns = sorted(
        {
            column_name
            for column_name in result_columns
            if result_columns.count(column_name) > 1
        }
    )
    if duplicate_columns:
        raise SqlValidationError(
            code="duplicate_result_columns",
            message="result_columns 不能包含重复列名",
            repair_action=(
                f"为重复列 {duplicate_columns} 设置不同的 AS 别名，并按最外层 SELECT "
                "顺序重写 result_columns。"
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
            message="result_columns 必须与最外层 SELECT 输出列名按顺序完全一致",
            repair_action=(
                "保持 SQL 不变，把 result_columns 精确改为："
                f"{actual_result_columns}；若其中存在空名称或重复名称，先为对应 SELECT "
                "表达式补充唯一 AS 别名。"
            ),
            details={
                "actual_result_columns": actual_result_columns,
                "declared_result_columns": result_columns,
            },
        )


# 从真实结构响应建立字段集合，供原料 SQL 在执行前拦截已限定的虚构字段。
def _build_material_schema_fields(
    schema_results: list[TableSchemaToolResponse],
) -> dict[str, frozenset[str]]:
    schema_fields: dict[str, frozenset[str]] = {}
    for schema_result in schema_results:
        if schema_result.status != "success" or schema_result.table_name is None:
            continue
        payload = parse_yaml_context(schema_result.result)
        columns = payload.get("columns") if isinstance(payload, dict) else None
        if not isinstance(columns, list):
            continue
        schema_fields[schema_result.table_name] = frozenset(
            str(column["field_name"])
            for column in columns
            if isinstance(column, dict) and "field_name" in column
        )
    return schema_fields


# 提取规划层已经用真实原名声明的字段，未写原名的业务描述继续由 SQL 模型结合结构解析。
def _extract_material_plan_field_references(
    material_plan: MaterialSqlQueryPlan,
) -> set[tuple[str, str]]:
    required_tables = set(material_plan.required_tables)
    return {
        (table_name, field_name)
        for table_name, field_name in re.findall(
            r"\b([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\b",
            material_plan.guidance,
        )
        if table_name in required_tables
    }


# 在调用模型前确认规划显式字段真实存在，使错误回到规划层而不是诱导 SQL 猜测替代字段。
def _validate_material_plan_schema_references(
    material_plan: MaterialSqlQueryPlan,
    schema_results: list[TableSchemaToolResponse],
) -> None:
    schema_fields = _build_material_schema_fields(schema_results)
    invalid_references = sorted(
        f"{table_name}.{field_name}"
        for table_name, field_name in _extract_material_plan_field_references(
            material_plan
        )
        if field_name not in schema_fields.get(table_name, frozenset())
    )
    if invalid_references:
        raise SqlValidationError(
            code="material_plan_unknown_field",
            message="原料查询计划引用了真实表结构中不存在的字段",
            repair_action=(
                f"返回查询规划层，删除或改正字段 {invalid_references}；"
                "重新读取对应表结构后再提交原料计划。"
            ),
            retry_target="query_planning",
            details={"invalid_field_references": invalid_references},
        )


# 核对基础表字段引用；CTE 和派生表输出继续交由 MySQL 解析，避免把合法中间列误判为物理字段。
def _validate_material_field_references(
    expression: exp.Expression,
    material_plan: MaterialSqlQueryPlan,
    schema_results: list[TableSchemaToolResponse],
    expected_table_names: set[str],
) -> None:
    schema_fields = _build_material_schema_fields(schema_results)
    missing_schemas = sorted(expected_table_names - set(schema_fields))
    if missing_schemas:
        raise SqlValidationError(
            code="material_schema_missing",
            message="SQL 校验缺少查询计划所需表结构",
            repair_action="停止 SQL 修复，由系统重新读取缺失表结构后再生成查询。",
            retry_target="none",
            details={"missing_tables": missing_schemas},
        )

    cte_names = {cte.alias_or_name for cte in expression.find_all(exp.CTE)}
    physical_alias_sources: dict[str, set[str]] = {}
    for table in expression.find_all(exp.Table):
        if table.name in cte_names:
            continue
        physical_alias_sources.setdefault(table.alias_or_name, set()).add(table.name)
    physical_aliases = {
        alias: next(iter(table_names))
        for alias, table_names in physical_alias_sources.items()
        if len(table_names) == 1
    }
    invalid_references: list[str] = []
    actual_physical_references: set[tuple[str, str]] = set()
    physical_table_set = set(physical_aliases.values())
    can_resolve_unqualified_columns = (
        len(physical_table_set) == 1 and not cte_names
    )
    for column in expression.find_all(exp.Column):
        source_table: str | None = None
        if column.table in physical_aliases:
            source_table = physical_aliases[column.table]
        elif not column.table and can_resolve_unqualified_columns:
            source_table = next(iter(physical_table_set))
        if source_table is None:
            continue
        actual_physical_references.add((source_table, column.name))
        if column.name not in schema_fields[source_table]:
            invalid_references.append(
                f"{column.table + '.' if column.table else ''}{column.name}"
            )
    if invalid_references:
        unique_references = sorted(set(invalid_references))
        raise SqlValidationError(
            code="unknown_schema_field",
            message="SQL 引用了真实表结构中不存在的字段",
            repair_action=(
                f"删除或改正字段 {unique_references}；只能使用 allowed_table_schemas "
                "中对应表实际列出的 field_name。"
            ),
            details={"invalid_field_references": unique_references},
        )
    missing_plan_references = sorted(
        f"{table_name}.{field_name}"
        for table_name, field_name in _extract_material_plan_field_references(
            material_plan
        )
        if (table_name, field_name) not in actual_physical_references
    )
    if missing_plan_references:
        raise SqlValidationError(
            code="material_plan_field_not_implemented",
            message="SQL 没有落实原料查询计划明确声明的真实字段",
            repair_action=(
                f"在保持原业务口径不变的前提下，让 SQL 实际引用字段 "
                f"{missing_plan_references}；不得用固定参数、相似字段或其他表字段替代。"
            ),
            details={"missing_field_references": missing_plan_references},
        )


# 读取三字段计划 SQL 自己声明的顶层分页，不推测自然语言中的数值口径。
def _resolve_material_effective_limit(expression: exp.Expression) -> int | None:
    limit_clause = expression.args.get("limit")
    if limit_clause is None:
        if expression.args.get("offset") is not None:
            raise SqlValidationError(
                code="offset_without_limit",
                message="SQL 不能在没有 LIMIT 时单独使用 OFFSET",
                repair_action="删除 OFFSET，或按原料查询指导同时提供明确的 LIMIT 整数。",
            )
        return None
    actual_limit = _get_integer_clause_value(limit_clause, "LIMIT", 0)
    _get_integer_clause_value(expression.args.get("offset"), "OFFSET", 0)
    return actual_limit


# 对模型草稿执行 AST 安全校验、分页约束和参数编译，返回可传给 asyncmy 的 SQL 与参数序列。
def validate_sql_draft(
    draft: SqlQueryDraft,
    query_plan: SqlInputPlan,
    allowed_table_names: frozenset[str],
    schema_results: list[TableSchemaToolResponse] | None = None,
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
    if isinstance(query_plan, MaterialSqlQueryPlan):
        _validate_material_field_references(
            expression,
            query_plan,
            schema_results or [],
            set(_get_plan_table_names(query_plan, allowed_table_names)),
        )
    else:
        _validate_query_block_implementation(resolved_expression, query_plan)
        _validate_quantified_condition_implementation(expression, draft, query_plan)
    _validate_external_filter_values_parameterized(expression)
    if isinstance(query_plan, MaterialSqlQueryPlan):
        _validate_material_result_columns(expression, draft.result_columns)
        _resolve_material_effective_limit(expression)
    else:
        _validate_result_columns(expression, draft.result_columns, query_plan)
        _resolve_effective_limit(expression, query_plan)
    normalized_sql = expression.sql(dialect="mysql")
    return _compile_named_parameters(normalized_sql, draft.parameters)


class AsyncMyReadOnlySqlExecutor:
    """通过独立的只读事务执行已通过静态校验的参数化 SQL。"""

    # 保存开发数据库配置，执行器只接收已校验的 SQL 和绑定参数而不接触模型输出对象。
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # 按 MySQL 错误码区分可由模型修复的查询问题，并为聚合错误提供唯一、无歧义的修复方向。
    @staticmethod
    def _resolve_query_repair(mysql_code: int | None) -> tuple[bool, str]:
        if mysql_code in {1055, 1140}:
            return (
                True,
                "当前 SQL 同时输出聚合表达式和非聚合列，但分组关系不完整。"
                "请为最外层 SELECT 的全部非聚合输出列补充 GROUP BY，"
                "或先在独立 CTE/子查询中按计划要求的主体粒度完成聚合后再关联明细；"
                "不得删除计划要求的结果列、放宽筛选条件或改变结果行粒度。",
            )
        repairable_codes = {1052, 1054, 1064, 1111, 1146, 1241, 1242}
        if mysql_code in repairable_codes:
            return (
                True,
                "依据数据库错误和已提供表结构修正 SQL 语法、字段限定或子查询写法，"
                "保持查询计划的筛选条件、结果列和结果行粒度不变。",
            )
        return (
            False,
            "无需修改 SQL；由系统检查数据库连接、权限或服务状态。",
        )

    # 异步执行受超时保护的只读查询，并把数据库异常归一为可判定的重试语义。
    async def execute(
        self,
        sql: str,
        parameters: tuple[SqlScalar, ...],
    ) -> list[dict[str, Any]]:
        try:
            return await asyncio.wait_for(
                self._execute_async(sql, parameters),
                timeout=QUERY_EXECUTION_TIMEOUT_SECONDS,
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
            retryable, repair_action = self._resolve_query_repair(mysql_code)
            raise SqlExecutionError(
                code=(
                    f"mysql_query_error_{mysql_code}"
                    if mysql_code is not None
                    else "mysql_query_error"
                ),
                message=f"数据库拒绝执行当前只读 SQL：{safe_message}",
                repair_action=repair_action,
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
        message_trace_queue: ModelMessageTraceQueue | None = None,
        progress_emitter: ProgressEmitter | None = None,
        max_tokens: int = DEFAULT_SQL_MAX_TOKENS,
        request_profile: str = "deepseek",
        tool_tag_template: str | None = None,
        close_client_after_run: bool = False,
    ) -> None:
        self._client = client
        self._model = model
        self._domain_profile = domain_profile
        self._allowed_table_names = frozenset(domain_profile.allowed_tables)
        self._schema_reader = schema_reader
        self._sql_executor = sql_executor
        self._trace_writer = trace_writer
        self._message_trace_queue = message_trace_queue
        self._progress_reporter = AgentProgressReporter(
            domain_profile,
            progress_emitter,
        )
        self._max_tokens = max_tokens
        self._request_profile = get_model_request_profile(request_profile)
        self._tool_tag_template = tool_tag_template
        self._close_client_after_run = close_client_after_run

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

    # 从全局供应商配置创建标准异步客户端、可复用结构读取器和只读 MySQL 执行器。
    @classmethod
    def from_settings(
        cls,
        domain_profile: QueryDomainProfile,
        settings: Settings | None = None,
        schema_reader: SchemaReader | None = None,
        trace_writer: TraceWriter | None = None,
        message_trace_queue: ModelMessageTraceQueue | None = None,
        progress_emitter: ProgressEmitter | None = None,
    ) -> "SqlQuerySubgraph":
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
        sql_executor = AsyncMyReadOnlySqlExecutor(resolved_settings)
        return cls(
            client=client,
            model=connection.model,
            domain_profile=domain_profile,
            schema_reader=schema_reader,
            sql_executor=sql_executor.execute,
            trace_writer=trace_writer,
            message_trace_queue=message_trace_queue,
            progress_emitter=progress_emitter,
            max_tokens=resolved_settings.deepseek_query_sql_max_tokens,
            request_profile=connection.provider,
            tool_tag_template=load_tool_tag_template(
                resolve_query_tool_tag_template_filename(resolved_settings)
            ),
            close_client_after_run=True,
        )

    # 在显式启用内部追踪时记录模型输出和校验结果，默认不写入标准输出。
    def _write_trace(self, content: str) -> None:
        if self._trace_writer is not None:
            self._trace_writer(content)

    # 异步桥接现有结构缓存读取器；任何表结构读取失败都会在调用模型前停止。
    async def _read_plan_schemas(
        self,
        query_plan: SqlInputPlan,
    ) -> list[TableSchemaToolResponse]:
        schema_results: list[TableSchemaToolResponse] = []
        for table_name in _get_plan_table_names(
            query_plan,
            self._allowed_table_names,
        ):
            schema_result = await asyncio.to_thread(self._schema_reader, table_name)
            schema_results.append(schema_result)
            if schema_result.status == "failure":
                raise RuntimeError(f"无法读取表 {table_name} 的结构：{schema_result.result}")
        return schema_results

    # 按计划顺序优先采用上游成功结构，缺失时异步桥接共享缓存按需读取。
    async def _resolve_plan_schemas(
        self,
        query_plan: SqlInputPlan,
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
                schema_result = await asyncio.to_thread(
                    self._schema_reader,
                    table_name,
                )
            if schema_result.status == "failure":
                raise RuntimeError(
                    f"无法读取表 {table_name} 的结构：{schema_result.result}"
                )
            resolved_schema_results.append(schema_result)
        return resolved_schema_results

    # 以 auto 模式异步生成唯一 SQL 工具调用，并把协议或参数错误作为可修复上下文返回。
    async def _generate_sql(self, state: _SqlQueryState) -> dict[str, Any]:
        generation_count = state.get("generation_count", 0)
        max_generation_count = state["max_generation_count"]
        raw_model_responses = list(state.get("raw_model_responses", []))
        repair_context_start = state.get("repair_context_start")
        repair_target = state.get("repair_target")
        try:
            schema_results = state.get("schema_results")
            messages = list(state.get("messages", []))
            if schema_results is None:
                provided_schema_results = state.get("provided_schema_results")
                if provided_schema_results is None:
                    schema_results = await self._read_plan_schemas(
                        state["query_plan"]
                    )
                else:
                    schema_results = await self._resolve_plan_schemas(
                        state["query_plan"],
                        provided_schema_results,
                    )
                if isinstance(state["query_plan"], MaterialSqlQueryPlan):
                    _validate_material_plan_schema_references(
                        state["query_plan"],
                        schema_results,
                    )
                if isinstance(state["query_plan"], MaterialSqlQueryPlan):
                    from app.agent.text2sql.subgraphs.sql.prompt.material_prompt import (
                        build_material_sql_generation_messages,
                    )

                    messages = build_material_sql_generation_messages(
                        state["query_plan"],
                        schema_results,
                        self._tool_tag_template,
                    )
                else:
                    messages = _build_sql_generation_messages(
                        state["query_plan"],
                        schema_results,
                        self._tool_tag_template,
                    )
            sql_tool = build_sql_query_tool_definition()
            generation_count += 1
            current_turn_start = len(messages)
            response = await create_traced_chat_completion(
                client=self._client,
                message_queue=self._message_trace_queue,
                node="sql",
                model=self._model,
                messages=list(messages),
                tools=[sql_tool],
                tool_choice="auto",
                **self._request_profile.build_non_thinking_options(self._max_tokens),
            )
            raw_model_responses.append(_serialize_raw_response(response))
            choice = response.choices[0]
            message = choice.message
            finish_reason = getattr(choice, "finish_reason", None)
            tool_calls = getattr(message, "tool_calls", None) or []
            if len(tool_calls) != 1:
                can_retry = generation_count < max_generation_count
                output_truncated = finish_reason == "length"
                error_code = (
                    "sql_generation_output_truncated"
                    if output_truncated
                    else "sql_generation_protocol_failed"
                )
                error_message = (
                    "SQL 生成输出达到长度上限，未形成完整工具调用"
                    if output_truncated
                    else "SQL 生成模型没有且仅调用一次 submit_sql_query"
                )
                repair_action = (
                    "使用更短的表别名和等价紧凑写法，在当前输出预算内提交完整 SQL 工具参数。"
                    if output_truncated
                    else "不要输出普通文本，只调用一次 submit_sql_query 并提交全部必填参数。"
                )
                messages.append(message)
                if can_retry:
                    if tool_calls:
                        for invalid_tool_call in tool_calls:
                            messages.append(
                                build_sql_protocol_retry_message(
                                    error_code,
                                    error_message,
                                    repair_action,
                                    invalid_tool_call.id,
                                )
                            )
                    else:
                        messages.append(
                            build_sql_protocol_retry_message(
                                error_code,
                                error_message,
                                repair_action,
                                tool_tag_template=self._tool_tag_template,
                            )
                        )
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                if repair_target in {None, "tool_protocol", "tool_arguments"}:
                    repair_target = "tool_protocol"
                self._write_trace(
                    "\n================ SQL 子图：工具协议失败 ================\n"
                    f"第 {generation_count}/{max_generation_count} 次生成\n"
                    f"完成原因：{finish_reason or '未知'}\n"
                    f"{error_message}\n"
                    f"{'将反馈给模型修复。' if can_retry else '已用尽生成次数。'}"
                )
                return {
                    "schema_results": schema_results,
                    "messages": messages,
                    "raw_model_responses": raw_model_responses,
                    "generation_count": generation_count,
                    "error": error_message,
                    "error_code": error_code,
                    "retry_target": "sql_generation",
                    "repair_context_start": repair_context_start,
                    "repair_target": repair_target,
                    "next_action": "generate_sql" if can_retry else "end",
                }
            tool_call = tool_calls[0]
            if tool_call.function.name != SUBMIT_SQL_QUERY_TOOL_NAME:
                can_retry = generation_count < max_generation_count
                error_message = (
                    "SQL 生成模型调用了未注册工具："
                    f"{tool_call.function.name}"
                )
                messages.append(message)
                if can_retry:
                    messages.append(
                        build_sql_protocol_retry_message(
                            "sql_generation_protocol_failed",
                            error_message,
                            "只调用 submit_sql_query，不得调用其他工具。",
                            tool_call.id,
                        )
                    )
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                if repair_target in {None, "tool_protocol", "tool_arguments"}:
                    repair_target = "tool_protocol"
                self._write_trace(
                    "\n================ SQL 子图：工具协议失败 ================\n"
                    f"第 {generation_count}/{max_generation_count} 次生成\n"
                    f"{error_message}\n"
                    f"{'将反馈给模型修复。' if can_retry else '已用尽生成次数。'}"
                )
                return {
                    "schema_results": schema_results,
                    "messages": messages,
                    "raw_model_responses": raw_model_responses,
                    "generation_count": generation_count,
                    "error": error_message,
                    "error_code": "sql_generation_protocol_failed",
                    "retry_target": "sql_generation",
                    "repair_context_start": repair_context_start,
                    "repair_target": repair_target,
                    "next_action": "generate_sql" if can_retry else "end",
                }
            if finish_reason == "length":
                can_retry = generation_count < max_generation_count
                error_message = "SQL 生成输出达到长度上限，工具参数不完整"
                messages.append(message)
                if can_retry:
                    messages.append(
                        build_sql_protocol_retry_message(
                            "sql_generation_output_truncated",
                            error_message,
                            (
                                "使用更短的表别名和等价紧凑写法，在当前输出预算内"
                                "重新提交完整 SQL、parameters 和 result_columns。"
                            ),
                            tool_call.id,
                        )
                    )
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                if repair_target in {None, "tool_protocol", "tool_arguments"}:
                    repair_target = "tool_protocol"
                self._write_trace(
                    "\n================ SQL 子图：工具参数截断 ================\n"
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
                    "error_code": "sql_generation_output_truncated",
                    "retry_target": "sql_generation",
                    "repair_context_start": repair_context_start,
                    "repair_target": repair_target,
                    "next_action": "generate_sql" if can_retry else "end",
                }
            messages.append(message)
            try:
                draft = parse_sql_query_tool_arguments(
                    tool_call.function.arguments
                )
            except ValidationError as error:
                argument_feedback = build_tool_argument_error_message(
                    tool_call.id,
                    SUBMIT_SQL_QUERY_TOOL_NAME,
                    error,
                )
                messages.append(argument_feedback)
                error_message = f"SQL 工具参数校验失败：{error}"
                can_retry = generation_count < max_generation_count
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                if repair_target in {None, "tool_protocol", "tool_arguments"}:
                    repair_target = "tool_arguments"
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
                    "repair_context_start": repair_context_start,
                    "repair_target": repair_target,
                    "next_action": "generate_sql" if can_retry else "end",
                }
            if repair_target in {"tool_protocol", "tool_arguments"}:
                current_turn_start = _clear_repaired_sql_context(
                    messages,
                    repair_context_start,
                    current_turn_start,
                )
                repair_context_start = None
                repair_target = None
                self._write_trace(
                    "\n================ SQL 子图：修复上下文清理 ================\n"
                    "工具协议或参数已修正；旧调用与错误反馈已从后续上下文移除。"
                )
            self._write_trace(
                "\n================ SQL 子图：生成 ================\n"
                f"第 {generation_count}/{max_generation_count} 次生成\n"
                f"模型工具参数：\n{tool_call.function.arguments}\n"
                "计划表："
                + ", ".join(
                    _get_plan_table_names(
                        state["query_plan"],
                        self._allowed_table_names,
                    )
                )
            )
            return {
                "schema_results": schema_results,
                "messages": messages,
                "draft": draft,
                "raw_model_responses": raw_model_responses,
                "generation_count": generation_count,
                "current_tool_call_id": tool_call.id,
                "current_turn_start": current_turn_start,
                "repair_context_start": repair_context_start,
                "repair_target": repair_target,
                "error": "",
                "error_code": "",
                "retry_target": "none",
                "next_action": "validate_sql",
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
                state.get("schema_results"),
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
            effective_limit = (
                _resolve_material_effective_limit(original_expression)
                if isinstance(state["query_plan"], MaterialSqlQueryPlan)
                else _resolve_effective_limit(
                    original_expression,
                    state["query_plan"],
                )
            )
            messages = list(state.get("messages", []))
            current_turn_start = state.get(
                "current_turn_start",
                len(messages),
            )
            repair_context_start = state.get("repair_context_start")
            repair_target = state.get("repair_target")
            if repair_target == "sql_validation":
                current_turn_start = _clear_repaired_sql_context(
                    messages,
                    repair_context_start,
                    current_turn_start,
                )
                repair_context_start = None
                repair_target = None
                self._write_trace(
                    "\n================ SQL 子图：修复上下文清理 ================\n"
                    "SQL 静态校验已通过；旧草稿与校验错误已从后续上下文移除。"
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
                "messages": messages,
                "current_turn_start": current_turn_start,
                "repair_context_start": repair_context_start,
                "repair_target": repair_target,
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
            repair_context_start = state.get("repair_context_start")
            repair_target = state.get("repair_target")
            if can_retry and repair_context_start is None:
                repair_context_start = state.get(
                    "current_turn_start",
                    max(len(messages) - 2, 0),
                )
            if can_retry and repair_target is None:
                repair_target = "sql_validation"
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
                "repair_context_start": repair_context_start,
                "repair_target": repair_target,
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

    # 在第三节点中异步执行已绑定参数的只读 SQL，可修复数据库错误按原调用 ID 反馈模型。
    async def _execute_sql(self, state: _SqlQueryState) -> dict[str, Any]:
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
            returned_rows = await _resolve_maybe_awaitable(
                self._sql_executor(
                    state["validated_sql"],
                    state["parameter_values"],
                )
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
                planned_limit=(
                    effective_limit
                    if isinstance(state["query_plan"], MaterialSqlQueryPlan)
                    else state["query_plan"].pagination.limit
                ),
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
                repair_context_start = state.get("repair_context_start")
                if repair_context_start is None:
                    repair_context_start = state.get(
                        "current_turn_start",
                        max(len(messages) - 2, 0),
                    )
                return {
                    "messages": messages,
                    "error": message,
                    "error_code": error.code,
                    "retry_target": "sql_generation",
                    "repair_context_start": repair_context_start,
                    "repair_target": "sql_execution",
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

    # 异步运行独立子图并校验预算；Pipeline 可复用规划结构，缺项由共享缓存按需补齐。
    async def run(
        self,
        query_plan: SqlInputPlan,
        schema_results: list[TableSchemaToolResponse] | None = None,
        max_generation_count: int = DEFAULT_SQL_GENERATION_COUNT,
    ) -> SqlQuerySubgraphResult:
        if max_generation_count < 1:
            raise ValueError("max_generation_count 必须大于或等于 1")
        try:
            state = await self._workflow.ainvoke(
                {
                    "query_plan": query_plan,
                    "provided_schema_results": schema_results,
                    "generation_count": 0,
                    "max_generation_count": max_generation_count,
                    "raw_model_responses": [],
                    "repair_context_start": None,
                    "repair_target": None,
                }
            )
        finally:
            if self._close_client_after_run:
                await self._client.close()
        if "result" in state:
            return state["result"]
        return self._build_failure_result(
            state,
            state.get("error", "SQL 子图未产生执行结果"),
        )
