"""使用单步 DeepSeek 子智能体生成并执行受限单表只读查询。"""

import asyncio
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final
from uuid import uuid4

import asyncmy
from asyncmy.cursors import DictCursor
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlglot import exp, parse
from sqlglot.errors import ParseError

from app.agent.domains.base import QueryDomainProfile
from app.core.config import Settings
from app.agent.runtime.entity_lookup import (
    EntityLookupConfig,
    EntityLookupConfiguration,
    find_entity_lookup_config,
    load_entity_lookup_configuration,
)
from app.agent.runtime.model_options import (
    DEFAULT_INSPECTION_MAX_TOKENS,
    build_non_thinking_completion_options,
)
from app.agent.runtime.yaml_context import parse_yaml_context, render_yaml_context
from app.agent.tools.strict_schema import build_strict_tool_definition
from app.agent.tools.table_inspection import (
    DataInspectionPurpose,
    TableDataInspectionResponse,
)
from app.agent.runtime.table_schema_reader import InformationSchemaTableSchemaReader
from app.agent.tools.table_schema import TableSchemaToolResponse, ensure_allowed_table_name
from app.agent.tools.argument_feedback import build_tool_argument_error_message


DEFAULT_MAX_INSPECTION_ROWS: Final[int] = 10
MAX_INSPECTION_ARGUMENT_REPAIR_COUNT: Final[int] = 1
SINGLE_TABLE_SELECT_TOOL_NAME: Final[str] = "execute_single_table_select"
MAX_INSPECTION_COLUMNS: Final[int] = 8
MAX_CELL_CHARACTERS: Final[int] = 300
MAX_RESULT_CHARACTERS: Final[int] = 6000
INSPECTION_QUERY_TIMEOUT_SECONDS: Final[float] = 10.0
FORBIDDEN_INSPECTION_FUNCTION_NAMES: Final[frozenset[str]] = frozenset(
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
TraceWriter = Callable[[str], None]
SchemaReader = Callable[[str], TableSchemaToolResponse]
EXACT_NAME_LOOKUP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<select>select\s+.+?\s+from\s+`?(?P<table>[a-z_]+)`?)"
    r"\s+where\s+`?name`?\s*=\s*'(?:''|[^'])*'"
    r"\s+(?P<order>order\s+by\s+.+)$",
    flags=re.IGNORECASE | re.DOTALL,
)


class SingleTableSelectArguments(BaseModel):
    """子智能体生成的单表读取 SQL。"""

    model_config = ConfigDict(extra="forbid")

    sql: str = Field(
        description="只读单表 SELECT SQL；必须显式选择少量字段并查询指定表"
    )


@dataclass(frozen=True)
class _SimilarEntityCandidate:
    """保存内部名称比较得到的有限候选，避免把全量扫描行交给规划模型。"""

    identifier: str
    display_name: str
    similarity: float
    edit_distance: int


# 基于 Pydantic 参数模型约束子智能体只能提交待校验的单表 SELECT。
def _build_single_table_select_tool_definition() -> dict[str, object]:
    return build_strict_tool_definition(
        tool_name=SINGLE_TABLE_SELECT_TOOL_NAME,
        description="提交一条满足约束的单表只读 SELECT SQL。",
        arguments_model=SingleTableSelectArguments,
    )


# 序列化单次子智能体响应，保留 Schema 修复前后的原始模型输出供受限诊断回放。
def _serialize_model_response(response: Any) -> str:
    model_dump_json = getattr(response, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json(indent=2)
    return repr(response)


# 合并一次检索中的全部模型响应，单次响应保持原格式，多次响应使用 JSON 数组。
def _serialize_model_responses(raw_responses: list[str]) -> str:
    if len(raw_responses) == 1:
        return raw_responses[0]
    serialized_responses: list[Any] = []
    for raw_response in raw_responses:
        try:
            serialized_responses.append(json.loads(raw_response))
        except json.JSONDecodeError:
            serialized_responses.append(raw_response)
    return json.dumps(serialized_responses, ensure_ascii=False, indent=2)


# 返回单表检索的统一页大小，行内字段和字符预算仍负责限制上下文体积。
def _get_max_inspection_rows(table_name: str) -> int:
    return DEFAULT_MAX_INSPECTION_ROWS


# 从单表检索 LIMIT 读取正整数常量，拒绝表达式、参数化值和超出固定页大小的结果范围。
def _read_inspection_limit(limit_clause: exp.Expression, table_name: str) -> int:
    limit_expression = limit_clause.args.get("expression")
    if not isinstance(limit_expression, exp.Literal) or limit_expression.is_string:
        raise ValueError("数据检索的 LIMIT 必须是正整数常量")
    try:
        limit_value = int(limit_expression.this)
    except (TypeError, ValueError) as error:
        raise ValueError("数据检索的 LIMIT 必须是正整数常量") from error
    if limit_value < 1 or limit_value > _get_max_inspection_rows(table_name):
        raise ValueError(
            f"数据检索每页最多返回 {_get_max_inspection_rows(table_name)} 行"
        )
    return limit_value


# 将子智能体 SQL 解析为单表 SELECT AST，并拒绝写入、跨表、子查询、通配字段、变量赋值和高风险函数。
def _validate_single_table_select(sql: str, table_name: str) -> str:
    normalized_sql = sql.strip()
    if not normalized_sql or any(
        marker in normalized_sql for marker in (";", "--", "/*", "*/", "#")
    ):
        raise ValueError("子智能体没有生成单条有效 SQL")
    try:
        expressions = parse(normalized_sql, read="mysql")
    except ParseError as error:
        raise ValueError("子智能体没有生成有效的 MySQL SELECT") from error
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Select):
        raise ValueError("数据检索仅允许一条 SELECT")
    expression = expressions[0]
    if expression.args.get("locks") or any(
        True for _ in expression.find_all(exp.Into, exp.Lock)
    ):
        raise ValueError("数据检索禁止锁定读取或 SELECT INTO")
    if any(
        True
        for _ in expression.find_all(exp.Join, exp.Subquery, exp.CTE, exp.PropertyEQ)
    ):
        raise ValueError("数据检索禁止 JOIN、子查询、CTE 或变量赋值")
    if any(True for _ in expression.find_all(exp.Star)):
        raise ValueError("数据检索必须显式选择需要的字段，禁止 SELECT *")
    if len(expression.expressions) > MAX_INSPECTION_COLUMNS:
        raise ValueError(f"数据检索最多返回 {MAX_INSPECTION_COLUMNS} 个字段")

    tables = list(expression.find_all(exp.Table))
    if len(tables) != 1 or tables[0].name != table_name:
        raise ValueError("数据检索 SQL 必须且只能读取指定的一张表")
    if tables[0].db or tables[0].catalog:
        raise ValueError("数据检索禁止指定数据库或目录名")
    for function in expression.find_all(exp.Anonymous):
        if function.name.upper() in FORBIDDEN_INSPECTION_FUNCTION_NAMES:
            raise ValueError(f"数据检索包含不允许的函数：{function.name.upper()}")
    if any(True for _ in expression.find_all(exp.CurrentSchema, exp.CurrentUser)):
        raise ValueError("数据检索禁止读取当前数据库或连接身份")
    if expression.args.get("offset") is not None:
        raise ValueError("数据检索的 OFFSET 只能由内部翻页器维护")
    limit_clause = expression.args.get("limit")
    if limit_clause is not None:
        _read_inspection_limit(limit_clause, table_name)
        expression.set("limit", None)
    return expression.sql(dialect="mysql")


# 仅对简单的名称精确匹配构造全量候选扫描，避免口语别名未命中时被误判为实体不存在。
def _build_name_lookup_fallback_sql(
    base_sql: str,
    table_name: str,
) -> str | None:
    matched = EXACT_NAME_LOOKUP_PATTERN.fullmatch(base_sql)
    if matched is None or matched.group("table").lower() != table_name:
        return None
    return f"{matched.group('select')} {matched.group('order')}"


# 归一化名称的 Unicode、大小写和分隔符差异，使拼写比较不受展示格式影响。
def _normalize_lookup_name(value: str) -> str:
    normalized_value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized_value if character.isalnum())


# 计算允许相邻字符交换的编辑距离，适合短名称的漏字、错字和相邻字母颠倒。
def _calculate_damerau_levenshtein_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous_previous_row: list[int] | None = None
    previous_row = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current_row = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            substitution_cost = 0 if left_character == right_character else 1
            current_value = min(
                previous_row[right_index] + 1,
                current_row[right_index - 1] + 1,
                previous_row[right_index - 1] + substitution_cost,
            )
            if (
                previous_previous_row is not None
                and left_index > 1
                and right_index > 1
                and left_character == right[right_index - 2]
                and left[left_index - 2] == right_character
            ):
                current_value = min(current_value, previous_previous_row[right_index - 2] + 1)
            current_row.append(current_value)
        previous_previous_row, previous_row = previous_row, current_row
    return previous_row[-1]


# 基于声明式字段配置构造受限轻量扫描 SQL；配置不是模型输入，因此不允许任意表名或字段名。
def _build_similarity_scan_sql(entity_config: EntityLookupConfig) -> str:
    return (
        f"SELECT `{entity_config.id_field}`, `{entity_config.display_field}` "
        f"FROM `{entity_config.table_name}` ORDER BY `{entity_config.id_field}`"
    )


# 在固定扫描预算内计算名称相似候选，仅返回少量标识和展示名称给规划模型。
def _find_similar_entity_candidates(
    rows: list[dict[str, Any]],
    lookup_value: str,
    entity_config: EntityLookupConfig,
) -> list[_SimilarEntityCandidate]:
    similarity_config = entity_config.similarity
    if similarity_config is None:
        return []
    normalized_lookup_value = _normalize_lookup_name(lookup_value)
    if not normalized_lookup_value:
        return []
    candidates: list[_SimilarEntityCandidate] = []
    for row in rows:
        display_name = row.get(entity_config.display_field)
        identifier = row.get(entity_config.id_field)
        if display_name is None or identifier is None:
            continue
        normalized_display_name = _normalize_lookup_name(str(display_name))
        if not normalized_display_name:
            continue
        edit_distance = _calculate_damerau_levenshtein_distance(
            normalized_lookup_value,
            normalized_display_name,
        )
        similarity = 1 - edit_distance / max(
            len(normalized_lookup_value), len(normalized_display_name)
        )
        if (
            edit_distance <= similarity_config.max_edit_distance
            and similarity >= similarity_config.threshold
        ):
            candidates.append(
                _SimilarEntityCandidate(
                    identifier=str(identifier),
                    display_name=str(display_name),
                    similarity=similarity,
                    edit_distance=edit_distance,
                )
            )
    candidates.sort(
        key=lambda candidate: (
            -candidate.similarity,
            candidate.edit_distance,
            candidate.display_name,
            candidate.identifier,
        )
    )
    return candidates[: similarity_config.max_candidates]


# 在 SQL 行数限制之外再压缩字段与字符长度，确保单次工具结果不会膨胀规划模型上下文。
def _compact_rows_for_context(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    compact_rows: list[dict[str, Any]] = []
    truncated = False
    for row in rows:
        compact_row: dict[str, Any] = {}
        for column_index, (column_name, value) in enumerate(row.items(), start=1):
            if column_index > MAX_INSPECTION_COLUMNS:
                truncated = True
                break
            rendered_value = str(value) if value is not None else None
            if rendered_value is not None and len(rendered_value) > MAX_CELL_CHARACTERS:
                rendered_value = rendered_value[:MAX_CELL_CHARACTERS] + "…"
                truncated = True
            compact_row[column_name] = rendered_value
        candidate_rows = [*compact_rows, compact_row]
        if len(json.dumps(candidate_rows, ensure_ascii=False)) > MAX_RESULT_CHARACTERS:
            truncated = True
            break
        compact_rows.append(compact_row)
    return compact_rows, truncated


# 将当前页候选行排版为紧凑 YAML，使模型直接关注字段和值而非 JSON 转义结构。
def _render_inspection_result(
    table_name: str,
    rows: list[dict[str, Any]],
    inspection_id: str,
    page_id: str,
    has_more: bool,
    message: str,
    truncated: bool,
    entity_config: EntityLookupConfig | None = None,
    lookup_value: str | None = None,
    similar_candidates: list[_SimilarEntityCandidate] | None = None,
) -> str:
    result: dict[str, Any] = {"table": table_name, "rows": rows}
    if entity_config is not None and lookup_value is not None:
        result["entity_lookup"] = {
            "entity_type": entity_config.entity_type,
            "lookup_value": lookup_value,
            "similar_candidates": [
                {
                    entity_config.id_field: candidate.identifier,
                    entity_config.display_field: candidate.display_name,
                    "match_basis": "normalized_damerau_levenshtein",
                    "similarity": round(candidate.similarity, 2),
                }
                for candidate in similar_candidates or []
            ],
        }
    result["page"] = {
        "inspection_id": inspection_id,
        "page_id": page_id,
        "has_more": has_more,
        "message": message,
    }
    if truncated:
        result["truncated"] = True
    return render_yaml_context(result)


# 构造带完整中文 YAML 字段说明的稳定单表检索前缀，仅把行数上限作为业务域配置注入。
def _build_single_table_select_system_prompt(table_name: str) -> str:
    return f"""你是受限的单表数据检索子智能体。根据用户需求和表结构生成一条 SQL。

只能查询指定的一张表，必须使用 SELECT，禁止 JOIN、子查询、写操作、注释、SELECT * 和高风险函数。只能显式选择必要字段，最多 8 个字段；目标表最多返回 {_get_max_inspection_rows(table_name)} 行。

如果结果可能需要翻页，使用目标表稳定的唯一标识或等价稳定字段 ORDER BY，以保证内部顺序翻页不会遗漏或重复候选。当用户用词可能是别名、无法据此安全构造筛选条件时，不能武断使用该用词过滤掉潜在候选；应读取识别实体所需字段并按稳定顺序扫描，供上层按页确认。

只调用 execute_single_table_select 工具。工具参数必须是合法 JSON；若 SQL 字符串内容需要引用业务值，使用单引号或中文引号，不要在 JSON 字符串内容中使用未转义的 ASCII 双引号。

# 输入 YAML 结构说明

- `inspection_request`：本次单表检索请求。
- `target_table`：唯一允许查询的真实表名。
- `table_schema`：目标表结构；`table` 是表名，`columns` 是字段列表。
- `field_name`、`data_type`、`foreign_key`、`comment`：分别是字段名、数据库类型、外键目标和数据库字段注释。
- `request`：上层需要本次检索确认的具体事实。"""


# 为同一单表 SQL 保存顺序翻页所需的游标、页数和是否仍有候选行。
@dataclass
class _InspectionSession:
    table_name: str
    base_sql: str
    next_offset: int
    page_count: int
    current_page_id: str
    has_more: bool


class SingleTableDataInspector:
    """为规划模型提供一次受限的实际表数据检索能力。"""

    # 保存模型、数据库配置和结构读取器，确保子智能体只能依据目标表的真实字段生成查询。
    def __init__(
        self,
        client: Any,
        model: str,
        settings: Settings,
        domain_profile: QueryDomainProfile,
        schema_reader: SchemaReader | None = None,
        trace_writer: TraceWriter | None = None,
        max_tokens: int = DEFAULT_INSPECTION_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model = model
        self._settings = settings
        self._domain_profile = domain_profile
        self._allowed_tables = frozenset(domain_profile.allowed_tables)
        self._entity_lookup_configuration: EntityLookupConfiguration = (
            load_entity_lookup_configuration(
                domain_profile.entity_lookup_config_path,
                domain_profile.allowed_tables,
            )
        )
        self._schema_reader = schema_reader or InformationSchemaTableSchemaReader(
            settings,
            domain_profile.allowed_tables,
        ).read
        self._trace_writer = trace_writer
        self._max_tokens = max_tokens
        self._inspection_sessions: dict[str, _InspectionSession] = {}

    # 在显式启用内部追踪时记录子智能体调用，默认不向标准输出泄漏模型内容。
    def _write_trace(self, content: str) -> None:
        if self._trace_writer is not None:
            self._trace_writer(content)

    # 创建一次受限单表检索；名称精确未命中时自动回退候选扫描，避免别名被误判为不存在。
    def inspect(
        self,
        table_name: str,
        request: str,
        lookup_value: str,
        generation_index: int,
        max_generation_count: int,
        purpose: DataInspectionPurpose = "entity_resolution",
    ) -> TableDataInspectionResponse:
        try:
            ensure_allowed_table_name(table_name, self._allowed_tables)
        except ValueError as error:
            return TableDataInspectionResponse(status="failure", result=str(error))
        schema_response = self._schema_reader(table_name)
        if schema_response.status == "failure":
            return TableDataInspectionResponse(
                status="failure",
                result=schema_response.result,
            )
        messages: list[Any] = [
            {
                "role": "system",
                "content": _build_single_table_select_system_prompt(table_name),
            },
            {
                "role": "user",
                "content": render_yaml_context(
                    {
                        "inspection_request": {
                            "target_table": table_name,
                            "table_schema": parse_yaml_context(
                                schema_response.result
                            ),
                            "request": request,
                        }
                    }
                ),
            },
        ]
        raw_responses: list[str] = []
        available_generation_count = max(max_generation_count - generation_index + 1, 1)
        max_attempt_count = min(
            MAX_INSPECTION_ARGUMENT_REPAIR_COUNT + 1,
            available_generation_count,
        )
        arguments: SingleTableSelectArguments | None = None

        for attempt_index in range(max_attempt_count):
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=[_build_single_table_select_tool_definition()],
                tool_choice={
                    "type": "function",
                    "function": {"name": SINGLE_TABLE_SELECT_TOOL_NAME},
                },
                **build_non_thinking_completion_options(self._max_tokens),
            )
            raw_responses.append(_serialize_model_response(response))
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            reasoning_content = getattr(message, "reasoning_content", None)
            content = getattr(message, "content", None)
            current_generation_index = generation_index + attempt_index
            self._write_trace(
                "\n"
                + "=" * 12
                + f" 第 {current_generation_index} / {max_generation_count} 次模型调用：单表数据检索子智能体 "
                + "=" * 12
                + (f"\n【模型思考】\n{reasoning_content}" if reasoning_content else "")
                + (f"\n【模型输出】\n{content}" if content else "")
                + "\n【工具调用】\n"
                + "\n".join(
                    f"- `{tool_call.function.name}`（{tool_call.id}）\n{tool_call.function.arguments}"
                    for tool_call in tool_calls
                )
            )
            if (
                len(tool_calls) != 1
                or tool_calls[0].function.name != SINGLE_TABLE_SELECT_TOOL_NAME
            ):
                return TableDataInspectionResponse(
                    status="failure",
                    result="单表数据检索子智能体未生成唯一的受限查询。",
                    raw_response=_serialize_model_responses(raw_responses),
                    model_generation_count=len(raw_responses),
                )
            tool_call = tool_calls[0]
            try:
                arguments = SingleTableSelectArguments.model_validate_json(
                    tool_call.function.arguments
                )
            except ValidationError as error:
                if attempt_index + 1 >= max_attempt_count:
                    return TableDataInspectionResponse(
                        status="failure",
                        result="单表数据检索工具参数未通过 Schema 校验。",
                        raw_response=_serialize_model_responses(raw_responses),
                        model_generation_count=len(raw_responses),
                    )
                messages.append(message)
                error_message = build_tool_argument_error_message(
                    tool_call.id,
                    SINGLE_TABLE_SELECT_TOOL_NAME,
                    error,
                )
                messages.append(error_message)
                self._write_trace(
                    "\n----- 单表数据检索工具参数校验结果 -----\n"
                    f"tool_call_id: {tool_call.id}\n"
                    f"tool_name: {SINGLE_TABLE_SELECT_TOOL_NAME}\n"
                    f"result: {error_message['content']}"
                )
                continue
            break

        assert arguments is not None
        raw_response = _serialize_model_responses(raw_responses)
        model_generation_count = len(raw_responses)
        try:
            base_sql = _validate_single_table_select(arguments.sql, table_name)
            inspection_id = uuid4().hex
            page_id = f"{inspection_id}:1"
            rows, database_has_more = self._query_page(base_sql, table_name, 0)
        except (ValueError, asyncmy.Error, OSError, RuntimeError):
            return TableDataInspectionResponse(
                status="failure",
                result="单表数据检索失败；请调整检索意图或检查数据库连接后重试。",
                raw_response=raw_response,
                model_generation_count=model_generation_count,
            )
        effective_base_sql = base_sql
        fallback_message: str | None = None
        entity_config = (
            find_entity_lookup_config(self._entity_lookup_configuration, table_name)
            if purpose == "entity_resolution"
            else None
        )
        similar_candidates: list[_SimilarEntityCandidate] = []
        if purpose == "entity_resolution" and not rows:
            fallback_sql = _build_name_lookup_fallback_sql(base_sql, table_name)
            if entity_config is not None and entity_config.match_mode == "name_fuzzy":
                assert entity_config.similarity is not None
                try:
                    candidate_rows = self._run_query_rows(
                        f"{_build_similarity_scan_sql(entity_config)} "
                        f"LIMIT {entity_config.similarity.max_scan_rows + 1}"
                    )
                    similar_candidates = _find_similar_entity_candidates(
                        candidate_rows[: entity_config.similarity.max_scan_rows],
                        lookup_value,
                        entity_config,
                    )
                except (asyncmy.Error, OSError, RuntimeError):
                    return TableDataInspectionResponse(
                        status="failure",
                        result="单表相似候选检索失败；请检查数据库连接后重试。",
                        raw_response=raw_response,
                        model_generation_count=model_generation_count,
                    )
            if fallback_sql is not None and entity_config is not None and entity_config.match_mode == "name_fuzzy":
                try:
                    rows, database_has_more = self._query_page(
                        fallback_sql,
                        table_name,
                        0,
                    )
                except (asyncmy.Error, OSError, RuntimeError):
                    return TableDataInspectionResponse(
                        status="failure",
                        result="单表候选回退检索失败；请检查数据库连接后重试。",
                        raw_response=raw_response,
                        model_generation_count=model_generation_count,
                    )
                effective_base_sql = fallback_sql
                fallback_message = "精确名称未命中，已自动回退为全量候选扫描。"
                if similar_candidates:
                    fallback_message += "已按实体配置计算全局相似候选，应先向用户确认。"
                else:
                    fallback_message += "未发现达到阈值的相似候选，可按页继续查找。"
        compact_rows, truncated = _compact_rows_for_context(rows)
        visible_row_count = len(compact_rows)
        has_more = database_has_more or visible_row_count < len(rows)
        self._inspection_sessions[inspection_id] = _InspectionSession(
            table_name=table_name,
            base_sql=effective_base_sql,
            next_offset=visible_row_count,
            page_count=1,
            current_page_id=page_id,
            has_more=has_more,
        )
        return TableDataInspectionResponse(
            status="success",
            result=_render_inspection_result(
                table_name,
                compact_rows,
                inspection_id,
                page_id,
                has_more,
                (
                    f"{fallback_message} 可继续查看下一页。"
                    if fallback_message and has_more
                    else f"{fallback_message} 已到达该检索结果的最后一页。"
                    if fallback_message
                    else "可继续查看下一页。"
                    if has_more
                    else "已到达该检索结果的最后一页。"
                ),
                truncated,
                entity_config=entity_config,
                lookup_value=lookup_value if entity_config is not None else None,
                similar_candidates=similar_candidates,
            ),
            inspection_id=inspection_id,
            page_id=page_id,
            has_more=has_more,
            sql=self._build_page_sql(effective_base_sql, table_name, 0),
            raw_response=raw_response,
            model_generation_count=model_generation_count,
        )

    # 按缓存游标读取下一页；到达末页后不再访问数据库并返回明确提示。
    def get_next_page(self, inspection_id: str) -> TableDataInspectionResponse:
        session = self._inspection_sessions.get(inspection_id)
        if session is None:
            return TableDataInspectionResponse(
                status="failure",
                result="单表检索结果不存在或已过期，无法继续翻页。",
            )
        if not session.has_more:
            return TableDataInspectionResponse(
                status="success",
                result=_render_inspection_result(
                    session.table_name,
                    [],
                    inspection_id,
                    session.current_page_id,
                    False,
                    "该单表检索结果已无后续页面。",
                    False,
                ),
                inspection_id=inspection_id,
                has_more=False,
            )
        try:
            rows, database_has_more = self._query_page(
                session.base_sql,
                session.table_name,
                session.next_offset,
            )
        except (asyncmy.Error, OSError, RuntimeError):
            return TableDataInspectionResponse(
                status="failure",
                result="单表检索下一页失败；请检查数据库连接后重试。",
            )
        compact_rows, truncated = _compact_rows_for_context(rows)
        visible_row_count = len(compact_rows)
        has_more = database_has_more or visible_row_count < len(rows)
        session.next_offset += visible_row_count
        session.page_count += 1
        page_id = f"{inspection_id}:{session.page_count}"
        session.current_page_id = page_id
        session.has_more = has_more
        return TableDataInspectionResponse(
            status="success",
            result=_render_inspection_result(
                session.table_name,
                compact_rows,
                inspection_id,
                page_id,
                has_more,
                "可继续查看下一页。" if has_more else "已到达该检索结果的最后一页。",
                truncated,
            ),
            inspection_id=inspection_id,
            page_id=page_id,
            has_more=has_more,
            sql=self._build_page_sql(
                session.base_sql,
                session.table_name,
                session.next_offset - visible_row_count,
            ),
        )

    # 在已校验的无分页 SQL 后附加固定页大小和内部 offset，额外读取一行判断是否还有下一页。
    def _query_page(
        self,
        base_sql: str,
        table_name: str,
        offset: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        page_size = _get_max_inspection_rows(table_name)
        raw_rows = self._run_query_rows(
            self._build_page_sql(base_sql, table_name, offset)
        )
        return raw_rows[:page_size], len(raw_rows) > page_size

    # 按内部游标构造带探测行的分页 SQL，禁止调用方控制 LIMIT 或 OFFSET。
    def _build_page_sql(
        self,
        base_sql: str,
        table_name: str,
        offset: int,
    ) -> str:
        return f"{base_sql} LIMIT {_get_max_inspection_rows(table_name) + 1} OFFSET {offset}"

    # 以同步工具接口执行带超时的异步读取，避免单个候选检索永久占用查询工作线程。
    def _run_query_rows(self, sql: str) -> list[dict[str, Any]]:
        return asyncio.run(
            asyncio.wait_for(
                self._query_rows(sql),
                timeout=INSPECTION_QUERY_TIMEOUT_SECONDS,
            )
        )

    # 在受限 SQL 已完成校验后执行只读查询，并以字典行返回少量候选数据。
    async def _query_rows(self, sql: str) -> list[dict[str, Any]]:
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
                await cursor.execute(sql)
                return await cursor.fetchall()
        finally:
            await connection.rollback()
            connection.close()
