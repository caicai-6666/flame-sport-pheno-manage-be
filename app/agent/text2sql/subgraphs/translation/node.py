"""定义查询结果状态值翻译子图的状态、节点及运行逻辑。"""

import json
import asyncio
from collections.abc import Callable, Collection
from typing import Any, Final, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlglot import exp, parse_one

from app.agent.text2sql.domains.base import QueryDomainProfile
from app.agent.text2sql.model_messages import (
    ModelMessageTraceQueue,
    create_traced_chat_completion,
)
from app.agent.text2sql.shared.yaml_context import parse_yaml_context, render_yaml_context
from app.agent.text2sql.shared.model_options import (
    DEFAULT_TRANSLATION_MAX_TOKENS,
    get_model_request_profile,
    resolve_model_provider_connection,
)
from app.agent.text2sql.shared.tool_tag_template import (
    load_tool_tag_template,
    resolve_query_tool_tag_template_filename,
)
from app.agent.text2sql.subgraphs.planning.tools.table_schema_cache import CachingTableSchemaReader
from app.agent.text2sql.subgraphs.planning.tools.table_schema_reader import InformationSchemaTableSchemaReader
from app.agent.text2sql.function_calling.feedback import (
    build_tool_argument_error_message,
)
from app.agent.text2sql.subgraphs.planning.tools.table_schema import (
    AllowedTableName,
    TableSchemaToolResponse,
)
from app.core.config import Settings, get_settings
from app.agent.text2sql.subgraphs.translation.tool import (
    SUBMIT_TRANSLATION_RULES_TOOL_NAME,
    SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
    build_translation_rules_tool_definition,
    build_translation_targets_tool_definition,
    parse_translation_rules_tool_arguments,
    parse_translation_targets_tool_arguments,
)
from app.agent.text2sql.subgraphs.translation.prompt import (
    TRANSLATION_RULE_SYSTEM_PROMPT,
    TRANSLATION_TARGET_SYSTEM_PROMPT,
)


RESULT_PREVIEW_ROW_COUNT: Final[int] = 5
MAX_TOOL_ARGUMENT_REPAIR_COUNT: Final[int] = 1
MAX_PARALLEL_TRANSLATION_FIELD_COUNT: Final[int] = 4
SchemaReader = Callable[[str], TableSchemaToolResponse]


class TranslationTarget(BaseModel):
    """节点 1 识别出的一个需要翻译且可追溯到直接来源列的结果字段。"""

    model_config = ConfigDict(extra="forbid")

    result_field: str = Field(description="最终 SQL 结果中的准确字段名或 AS 别名")
    source_table: AllowedTableName = Field(description="该结果字段直接来源的真实表名")
    source_field: str = Field(description="该结果字段直接来源的真实字段名")
    reason: str = Field(description="该字段为何属于需要翻译的枚举、状态或布尔编码")


class TranslationTargetSubmission(BaseModel):
    """节点 1 通过 Function Calling 提交的全部翻译目标，允许没有待翻译字段。"""

    model_config = ConfigDict(extra="forbid")

    targets: list[TranslationTarget] = Field(description="需要翻译的结果字段；没有时为空数组")


class TranslationMappingItem(BaseModel):
    """字段注释中一个原始数据库值与中文展示值的确定映射。"""

    model_config = ConfigDict(extra="forbid")

    raw_value: str = Field(description="注释中定义的原始数据库值，保持原文")
    display_value: str = Field(description="注释中定义的对应业务含义，保持原文")


class ColumnTranslationRule(BaseModel):
    """节点 2 针对一个结果字段生成的完整注释翻译规则。"""

    model_config = ConfigDict(extra="forbid")

    result_field: str = Field(description="要应用翻译的最终 SQL 结果字段")
    source_table: AllowedTableName = Field(description="字段注释所属的真实表名")
    source_field: str = Field(description="字段注释所属的真实字段名")
    mappings: list[TranslationMappingItem] = Field(
        description="只从字段注释逐项提取的原始值与展示值映射"
    )
    unknown_value_strategy: Literal["keep_original"] = Field(
        description="注释未定义的值必须保留原值"
    )


class TranslationRuleSubmission(BaseModel):
    """节点 2 通过 Function Calling 提交的全部字段翻译规则。"""

    model_config = ConfigDict(extra="forbid")

    translations: list[ColumnTranslationRule] = Field(
        description="能够从注释可靠提取的翻译规则；无法提取时省略对应字段"
    )


class ColumnCommentFact(BaseModel):
    """节点 2 从结构读取器取得的单个字段注释事实。"""

    model_config = ConfigDict(extra="forbid")

    result_field: str = Field(description="最终 SQL 结果中的字段名或别名")
    source_table: AllowedTableName = Field(description="数据库注释所属的真实表名")
    source_field: str = Field(description="数据库注释所属的真实字段名")
    comment: str = Field(description="从 information_schema 读取的原始字段注释")


class ResultTranslationSubgraphResult(BaseModel):
    """翻译子图的目标、注释事实、规则和可安全降级的完整翻译结果。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "failure"] = "success"
    targets: list[TranslationTarget] = Field(default_factory=list)
    comment_facts: list[ColumnCommentFact] = Field(default_factory=list)
    rules: list[ColumnTranslationRule] = Field(default_factory=list)
    original_rows: list[dict[str, Any]] = Field(default_factory=list)
    translated_rows: list[dict[str, Any]] = Field(default_factory=list)
    target_detection_raw_responses: list[str] = Field(default_factory=list)
    rule_generation_raw_responses: list[str] = Field(default_factory=list)
    error: str | None = Field(
        default=None,
        description="翻译失败时的内部错误摘要；正式 HTTP 响应不会直接暴露",
    )


class TranslationToolValidationError(ValueError):
    """携带稳定错误码、字段路径和唯一修复动作的翻译工具语义校验异常。"""

    # 保存可直接回传模型的精确错误事实，避免重试依赖模糊异常文本猜测修复方向。
    def __init__(
        self,
        code: str,
        field_path: str,
        message: str,
        repair_action: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field_path = field_path
        self.repair_action = repair_action


class _SchemaField(BaseModel):
    """从紧凑结构响应恢复的内部字段事实，不直接暴露给模型工具参数。"""

    field_name: str
    data_type: str
    foreign_key: str | None
    comment: str | None


class _ResultTranslationState(TypedDict, total=False):
    """三个翻译节点间只传递 SQL、结构、受限样本、规则与完整原始行。"""

    sql: str
    result_columns: list[str]
    rows: list[dict[str, Any]]
    result_column_sources: dict[str, str] | None
    provided_schema_results: list[TableSchemaToolResponse] | None
    schema_results: list[TableSchemaToolResponse]
    schema_fields: dict[str, dict[str, _SchemaField]]
    direct_lineage: dict[str, tuple[str, str]]
    targets: list[TranslationTarget]
    comment_facts: list[ColumnCommentFact]
    rules: list[ColumnTranslationRule]
    translated_rows: list[dict[str, Any]]
    target_detection_raw_responses: list[str]
    rule_generation_raw_responses: list[str]


# 将 OpenAI 兼容响应完整序列化，便于实验回放字段识别和注释解析行为。
def _serialize_raw_response(response: Any) -> str:
    model_dump_json = getattr(response, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json(indent=2)
    return repr(response)


# 从既有紧凑表结构结果恢复字段、类型、外键与注释，避免实验改动公共工具响应契约。
def _parse_schema_fields(
    schema_result: TableSchemaToolResponse,
) -> dict[str, _SchemaField]:
    if schema_result.status != "success" or schema_result.table_name is None:
        raise RuntimeError(
            f"表 {schema_result.table_name or '-'} 的结构读取失败：{schema_result.result}"
        )
    parsed_result = parse_yaml_context(schema_result.result)
    if not isinstance(parsed_result, dict):
        raise RuntimeError(f"表 {schema_result.table_name} 的 YAML 结构不是对象")
    if parsed_result.get("table") != schema_result.table_name:
        raise RuntimeError(f"表 {schema_result.table_name} 的 YAML 表名不一致")
    raw_columns = parsed_result.get("columns")
    if not isinstance(raw_columns, list):
        raise RuntimeError(f"表 {schema_result.table_name} 的 YAML 缺少 columns 列表")
    fields: dict[str, _SchemaField] = {}
    for raw_column in raw_columns:
        field = _SchemaField.model_validate(raw_column)
        fields[field.field_name] = field
    if not fields:
        raise RuntimeError(f"表 {schema_result.table_name} 的结构结果未包含可解析字段")
    return fields


# 解析 SQL 中的真实表和别名，节点 1 只允许为直接返回的原始列建立来源关系。
def _extract_direct_result_lineage(sql: str) -> tuple[list[str], dict[str, tuple[str, str]]]:
    expression = parse_one(sql, dialect="mysql")
    top_level_select = expression if isinstance(expression, exp.Select) else None
    cte_names = {
        cte.alias_or_name
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }
    table_names: list[str] = []
    top_level_alias_to_table: dict[str, str] = {}
    for table in expression.find_all(exp.Table):
        table_name = table.name
        if table_name in cte_names:
            continue
        if table_name not in table_names:
            table_names.append(table_name)
        if (
            top_level_select is not None
            and table.find_ancestor(exp.Select) is top_level_select
        ):
            top_level_alias_to_table[table.alias_or_name] = table_name
            top_level_alias_to_table[table_name] = table_name

    direct_lineage: dict[str, tuple[str, str]] = {}
    if top_level_select is None:
        return table_names, direct_lineage
    unique_top_level_tables = set(top_level_alias_to_table.values())
    for selection in top_level_select.selects:
        output_name = selection.alias_or_name
        selected_expression = selection.this if isinstance(selection, exp.Alias) else selection
        if not output_name or not isinstance(selected_expression, exp.Column):
            continue
        source_table = top_level_alias_to_table.get(selected_expression.table)
        if (
            source_table is None
            and not selected_expression.table
            and len(unique_top_level_tables) == 1
        ):
            source_table = next(iter(unique_top_level_tables))
        if source_table is not None:
            direct_lineage[output_name] = (source_table, selected_expression.name)
    return table_names, direct_lineage


# 渲染不含注释的结构上下文，让节点 1 只判断字段类型和来源，避免提前承担注释解析职责。
def _render_schema_without_comments(
    schema_fields: dict[str, dict[str, _SchemaField]],
) -> dict[str, list[dict[str, Any]]]:
    tables: list[dict[str, Any]] = []
    for table_name, fields in schema_fields.items():
        tables.append(
            {
                "table": table_name,
                "columns": [
                    {
                        "field_name": field.field_name,
                        "data_type": field.data_type,
                        "foreign_key": field.foreign_key,
                    }
                    for field in fields.values()
                ],
            }
        )
    return {"tables": tables}


# 将工具协议或语义校验错误作为同一 tool_call_id 的正常失败结果返回，允许模型有限修复。
def _build_semantic_tool_error_message(
    tool_call_id: str,
    tool_name: str,
    error: TranslationToolValidationError,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": error.code,
                    "tool_name": tool_name,
                    "message": "工具参数未通过翻译层语义校验，本次工具调用未执行。",
                    "details": [
                        {
                            "error_type": "business_rule_violation",
                            "field_path": error.field_path,
                            "message": str(error),
                            "repair_action": error.repair_action,
                        }
                    ],
                },
                "retryable": True,
                "next_action": (
                    f"执行 details 中唯一的 repair_action，然后重新调用 {tool_name}。"
                ),
            },
            ensure_ascii=False,
        ),
    }


# 在模型没有产生可回应的 tool_call_id 时追加协议校验反馈，并按配置附加工具标签格式。
def _build_missing_tool_call_feedback_message(
    tool_name: str,
    error: TranslationToolValidationError,
    tool_tag_template: str | None,
) -> dict[str, str]:
    feedback: dict[str, Any] = {
        "code": error.code,
        "tool_name": tool_name,
        "field_path": error.field_path,
        "message": str(error),
        "repair_action": error.repair_action,
    }
    if tool_tag_template is not None:
        feedback["tool_call_format_guidance"] = {
            "instruction": (
                "template 只展示当前模型的标签语法，严禁照抄其中示例工具名、"
                f"参数名或参数值；本轮必须输出唯一 {tool_name} 调用，"
                "并按本轮工具 Schema 生成完整 JSON 参数。"
            ),
            "template": tool_tag_template,
        }
    return {
        "role": "user",
        "content": json.dumps(
            {
                "context_type": "workflow_protocol_feedback",
                "status": "failure",
                "error": feedback,
                "retryable": True,
                "next_action": f"修正后只调用一次 {tool_name}。",
            },
            ensure_ascii=False,
        ),
    }


# 将错误工具名或多工具调用作为原调用 ID 的失败结果，避免 auto 模式静默接受普通文本。
def _build_protocol_tool_error_message(
    tool_call_id: str,
    called_tool_name: str,
    tool_name: str,
    error_code: str,
    message: str,
) -> dict[str, str]:
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
                    "repair_action": (
                        f"重新生成本轮响应，并且只调用一次 {tool_name}。"
                    ),
                },
                "retryable": True,
                "next_action": f"只调用一次 {tool_name}。",
            },
            ensure_ascii=False,
        ),
    }


# 组装通用 tool-tag 与动态 YAML 任务；模板只说明标签语法，具体工具完全来自当轮 Schema。
def _build_translation_messages(
    system_prompt: str,
    context: dict[str, Any],
    tool_tag_template: str | None,
) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": system_prompt}]
    if tool_tag_template is not None:
        messages.append(
            {
                "role": "user",
                "content": (
                    "# 工具调用标签语法\n\n"
                    "本任务必须通过当轮已注册工具推进。以下模板只说明服务端要求的"
                    "标签语法，不定义任何具体工具或业务参数；必须用当轮 Function "
                    "Calling Schema 中的真实工具名、参数名和参数值替换全部占位符，"
                    "严禁原样输出占位符。\n\n"
                    f"{tool_tag_template}"
                ),
            }
        )
    messages.append({"role": "user", "content": render_yaml_context(context)})
    return messages


# 将数据库标量统一为注释映射键；未知值不会被猜测或隐式转型为其他状态。
def _normalize_mapping_key(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class ResultTranslationSubgraph:
    """以两个受约束模型节点和一个确定性程序节点翻译查询结果中的状态编码。"""

    # 初始化异步模型、结构缓存与固定三节点工作流，完整 SQL 结果只在程序翻译节点遍历一次。
    def __init__(
        self,
        client: Any,
        model: str,
        schema_reader: SchemaReader,
        allowed_tables: Collection[str],
        max_tokens: int = DEFAULT_TRANSLATION_MAX_TOKENS,
        max_parallel_fields: int = MAX_PARALLEL_TRANSLATION_FIELD_COUNT,
        trace_writer: Callable[[str], None] | None = None,
        message_trace_queue: ModelMessageTraceQueue | None = None,
        request_profile: str = "deepseek",
        tool_tag_template: str | None = None,
        close_client_after_run: bool = False,
    ) -> None:
        if not allowed_tables:
            raise ValueError("翻译子图至少需要一张允许表")
        if max_parallel_fields < 1:
            raise ValueError("翻译规则并行数必须大于零")
        self._client = client
        self._model = model
        self._schema_reader = schema_reader
        self._allowed_tables = tuple(dict.fromkeys(allowed_tables))
        self._allowed_table_names = frozenset(self._allowed_tables)
        self._max_tokens = max_tokens
        self._max_parallel_fields = max_parallel_fields
        self._trace_writer = trace_writer
        self._message_trace_queue = message_trace_queue
        self._request_profile = get_model_request_profile(request_profile)
        self._tool_tag_template = tool_tag_template
        self._close_client_after_run = close_client_after_run

        workflow = StateGraph(_ResultTranslationState)
        workflow.add_node("identify_translation_targets", self._identify_translation_targets)
        workflow.add_node("build_translation_rules", self._build_translation_rules)
        workflow.add_node("apply_translations", self._apply_translations)
        workflow.add_edge(START, "identify_translation_targets")
        workflow.add_edge("identify_translation_targets", "build_translation_rules")
        workflow.add_edge("build_translation_rules", "apply_translations")
        workflow.add_edge("apply_translations", END)
        self._workflow = workflow.compile()

    # 从业务域与全局供应商配置创建标准异步客户端和白名单结构读取器，正式链路可注入共享缓存。
    @classmethod
    def from_settings(
        cls,
        domain_profile: QueryDomainProfile,
        settings: Settings | None = None,
        schema_reader: SchemaReader | None = None,
        trace_writer: Callable[[str], None] | None = None,
        message_trace_queue: ModelMessageTraceQueue | None = None,
    ) -> "ResultTranslationSubgraph":
        resolved_settings = settings or get_settings()
        connection = resolve_model_provider_connection(resolved_settings)
        client = AsyncOpenAI(
            api_key=connection.api_key,
            base_url=connection.base_url,
            timeout=connection.timeout_seconds,
        )
        cached_reader = schema_reader or CachingTableSchemaReader(
            InformationSchemaTableSchemaReader(
                resolved_settings,
                domain_profile.allowed_tables,
            ).read
        ).read
        return cls(
            client=client,
            model=connection.model,
            schema_reader=cached_reader,
            allowed_tables=domain_profile.allowed_tables,
            max_tokens=resolved_settings.deepseek_query_translation_max_tokens,
            max_parallel_fields=(
                resolved_settings.agent_query_translation_max_parallel_fields
            ),
            trace_writer=trace_writer,
            message_trace_queue=message_trace_queue,
            request_profile=connection.provider,
            tool_tag_template=load_tool_tag_template(
                resolve_query_tool_tag_template_filename(resolved_settings)
            ),
            close_client_after_run=True,
        )

    # 输出每个模型节点的轮次和工具参数，便于 IDE 实验直接观察字段识别及规则提取。
    def _write_trace(self, content: str) -> None:
        if self._trace_writer is not None:
            self._trace_writer(content)

    # 优先复用上游已读取结构；缺失时在线程中补读 SQL 实际涉及表，避免阻塞异步模型流程。
    async def _load_schema_context(
        self,
        table_names: list[str],
        provided_schema_results: list[TableSchemaToolResponse] | None,
    ) -> tuple[list[TableSchemaToolResponse], dict[str, dict[str, _SchemaField]]]:
        provided_by_table = {
            result.table_name: result
            for result in provided_schema_results or []
            if result.table_name is not None and result.status == "success"
        }
        schema_results: list[TableSchemaToolResponse] = []
        schema_fields: dict[str, dict[str, _SchemaField]] = {}
        for table_name in table_names:
            if table_name not in self._allowed_table_names:
                raise ValueError(f"SQL 涉及不允许的表：{table_name}")
            schema_result = provided_by_table.get(table_name)
            if schema_result is None:
                schema_result = await asyncio.to_thread(
                    self._schema_reader,
                    table_name,
                )
            schema_results.append(schema_result)
            schema_fields[table_name] = _parse_schema_fields(schema_result)
        return schema_results, schema_fields

    # 校验节点 1 目标严格对应 SQL 直接返回列，禁止模型猜测表达式、别名或跨表字段来源。
    def _validate_targets(
        self,
        submission: TranslationTargetSubmission,
        result_columns: list[str],
        direct_lineage: dict[str, tuple[str, str]],
        schema_fields: dict[str, dict[str, _SchemaField]],
    ) -> None:
        seen_fields: set[str] = set()
        for target_index, target in enumerate(submission.targets):
            target_path = f"targets.{target_index}"
            if target.result_field in seen_fields:
                raise TranslationToolValidationError(
                    code="duplicate_translation_target",
                    field_path=f"{target_path}.result_field",
                    message=f"结果字段 {target.result_field} 被重复提交。",
                    repair_action=f"删除重复的 {target_path}，只保留该结果字段第一次出现的目标项。",
                )
            seen_fields.add(target.result_field)
            if target.result_field not in result_columns:
                raise TranslationToolValidationError(
                    code="unknown_result_field",
                    field_path=f"{target_path}.result_field",
                    message=f"结果字段 {target.result_field} 不属于 SQL 返回列。",
                    repair_action=f"删除 {target_path}；只能从 result_columns 中选择待翻译字段。",
                )
            expected_source = direct_lineage.get(target.result_field)
            actual_source = (target.source_table, target.source_field)
            if expected_source != actual_source:
                if expected_source is None:
                    raise TranslationToolValidationError(
                        code="non_direct_result_expression",
                        field_path=target_path,
                        message=(
                            f"结果字段 {target.result_field} 不是可追溯的直接原始列，"
                            "不能进入翻译层。"
                        ),
                        repair_action=f"删除 {target_path}。",
                    )
                raise TranslationToolValidationError(
                    code="result_field_lineage_mismatch",
                    field_path=target_path,
                    message=(
                        f"结果字段 {target.result_field} 的直接来源是 "
                        f"{expected_source[0]}.{expected_source[1]}，实际提交 "
                        f"{actual_source[0]}.{actual_source[1]}。"
                    ),
                    repair_action=(
                        f"将 {target_path}.source_table 改为 {expected_source[0]}，"
                        f"并将 {target_path}.source_field 改为 {expected_source[1]}。"
                    ),
                )
            if target.source_field not in schema_fields.get(target.source_table, {}):
                raise TranslationToolValidationError(
                    code="source_field_not_found",
                    field_path=f"{target_path}.source_field",
                    message=(
                        f"字段 {target.source_table}.{target.source_field} "
                        "不存在于已读取表结构。"
                    ),
                    repair_action=f"删除 {target_path}。",
                )

    # 节点 1 以 auto Function Calling 识别编码字段，并把协议、参数和来源错误作为可修复结果返回。
    async def _identify_translation_targets(
        self,
        state: _ResultTranslationState,
    ) -> dict[str, Any]:
        table_names, sql_direct_lineage = _extract_direct_result_lineage(state["sql"])
        result_column_sources = state.get("result_column_sources")
        direct_lineage = sql_direct_lineage
        if result_column_sources is not None:
            direct_lineage = {
                result_field: sql_direct_lineage[source_result_field]
                for result_field, source_result_field in result_column_sources.items()
                if source_result_field in sql_direct_lineage
            }
        schema_results, schema_fields = await self._load_schema_context(
            table_names,
            state.get("provided_schema_results"),
        )
        context = {
            "executed_sql": state["sql"],
            "result_columns": state["result_columns"],
            "direct_column_lineage": {
                result_field: {
                    "source_table": source[0],
                    "source_field": source[1],
                }
                for result_field, source in direct_lineage.items()
            },
            "table_schemas_without_comments": _render_schema_without_comments(
                schema_fields
            ),
            "rows_preview": state["rows"][:RESULT_PREVIEW_ROW_COUNT],
        }
        messages: list[Any] = _build_translation_messages(
            TRANSLATION_TARGET_SYSTEM_PROMPT,
            context,
            self._tool_tag_template,
        )
        tool = build_translation_targets_tool_definition(self._allowed_tables)
        raw_responses: list[str] = []
        last_error: Exception | None = None
        repair_context_start: int | None = None
        for attempt in range(MAX_TOOL_ARGUMENT_REPAIR_COUNT + 1):
            current_turn_start = len(messages)
            self._write_trace(
                f"[翻译层节点 1] 第 {attempt + 1} 次模型调用：识别待翻译字段"
            )
            response = await create_traced_chat_completion(
                client=self._client,
                message_queue=self._message_trace_queue,
                node="translation.target_detection",
                model=self._model,
                messages=[*messages],
                tools=[tool],
                tool_choice="auto",
                **self._request_profile.build_non_thinking_options(
                    self._max_tokens,
                ),
            )
            raw_responses.append(_serialize_raw_response(response))
            choice = response.choices[0]
            message = choice.message
            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                last_error = TranslationToolValidationError(
                    code=(
                        "translation_target_tool_call_truncated"
                        if getattr(choice, "finish_reason", None) == "length"
                        else "translation_target_tool_call_missing"
                    ),
                    field_path="$",
                    message=(
                        "本轮输出达到长度上限且没有形成可解析工具调用。"
                        if getattr(choice, "finish_reason", None) == "length"
                        else "本轮没有调用工具，普通文本不能提交翻译目标。"
                    ),
                    repair_action=(
                        "缩短 reason，并只调用一次 submit_translation_targets。"
                        if getattr(choice, "finish_reason", None) == "length"
                        else "只调用一次 submit_translation_targets；没有待翻译字段时提交 targets 空数组。"
                    ),
                )
                if attempt >= MAX_TOOL_ARGUMENT_REPAIR_COUNT:
                    break
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                messages.extend(
                    (
                        message,
                        _build_missing_tool_call_feedback_message(
                            SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
                            last_error,
                            self._tool_tag_template,
                        ),
                    )
                )
                self._write_trace(
                    "[翻译层节点 1] 工具协议校验失败："
                    + messages[-1]["content"]
                )
                continue

            messages.append(message)
            if len(tool_calls) != 1:
                last_error = TranslationToolValidationError(
                    code="translation_target_multiple_tool_calls",
                    field_path="$",
                    message=(
                        "翻译目标识别每轮必须且只能调用一个工具，"
                        f"本轮实际调用 {len(tool_calls)} 个。"
                    ),
                    repair_action="重新生成本轮响应，并且只调用一次 submit_translation_targets。",
                )
                if attempt >= MAX_TOOL_ARGUMENT_REPAIR_COUNT:
                    break
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                messages.extend(
                    _build_protocol_tool_error_message(
                        tool_call.id,
                        tool_call.function.name,
                        SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
                        last_error.code,
                        str(last_error),
                    )
                    for tool_call in tool_calls
                )
                self._write_trace(
                    "[翻译层节点 1] 工具数量校验失败："
                    + messages[-1]["content"]
                )
                continue

            tool_call = tool_calls[0]
            if tool_call.function.name != SUBMIT_TRANSLATION_TARGETS_TOOL_NAME:
                last_error = TranslationToolValidationError(
                    code="translation_target_unexpected_tool",
                    field_path="$",
                    message=(
                        f"工具 {tool_call.function.name} 未在翻译目标识别节点注册，"
                        "本次调用未执行。"
                    ),
                    repair_action="只调用一次 submit_translation_targets。",
                )
                if attempt >= MAX_TOOL_ARGUMENT_REPAIR_COUNT:
                    break
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                messages.append(
                    _build_protocol_tool_error_message(
                        tool_call.id,
                        tool_call.function.name,
                        SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
                        last_error.code,
                        str(last_error),
                    )
                )
                self._write_trace(
                    "[翻译层节点 1] 工具名称校验失败："
                    + messages[-1]["content"]
                )
                continue

            try:
                parsed = parse_translation_targets_tool_arguments(
                    tool_call.function.arguments,
                )
                self._validate_targets(
                    parsed,
                    state["result_columns"],
                    direct_lineage,
                    schema_fields,
                )
                if repair_context_start is not None:
                    self._write_trace(
                        "[翻译层节点 1] 已修正工具调用；节点结束后丢弃独立修复上下文"
                    )
                self._write_trace(
                    "[翻译层节点 1] 已识别字段："
                    + (", ".join(item.result_field for item in parsed.targets) or "无")
                )
                return {
                    "schema_results": schema_results,
                    "schema_fields": schema_fields,
                    "direct_lineage": direct_lineage,
                    "targets": parsed.targets,
                    "target_detection_raw_responses": raw_responses,
                }
            except (ValidationError, TranslationToolValidationError) as error:
                last_error = error
                if attempt >= MAX_TOOL_ARGUMENT_REPAIR_COUNT:
                    break
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                if isinstance(error, ValidationError):
                    messages.append(
                        build_tool_argument_error_message(
                            tool_call.id,
                            SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
                            error,
                        )
                    )
                else:
                    messages.append(
                        _build_semantic_tool_error_message(
                            tool_call.id,
                            SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
                            error,
                        )
                    )
                self._write_trace(
                    "[翻译层节点 1] 工具参数或语义校验失败："
                    + messages[-1]["content"]
                )
        raise RuntimeError(f"翻译目标识别失败：{last_error}")

    # 从节点 1 精确目标中单独提取 comment，禁止把其他字段注释带入规则生成上下文。
    def _build_comment_facts(
        self,
        targets: list[TranslationTarget],
        schema_fields: dict[str, dict[str, _SchemaField]],
    ) -> list[ColumnCommentFact]:
        facts: list[ColumnCommentFact] = []
        for target in targets:
            field = schema_fields[target.source_table][target.source_field]
            if field.comment:
                facts.append(
                    ColumnCommentFact(
                        result_field=target.result_field,
                        source_table=target.source_table,
                        source_field=target.source_field,
                        comment=field.comment,
                    )
                )
        return facts

    # 校验节点 2 的每条映射都能在原始 comment 中找到，未知或模型外推含义不得进入程序翻译。
    def _validate_rules(
        self,
        submission: TranslationRuleSubmission,
        targets: list[TranslationTarget],
        comment_facts: list[ColumnCommentFact],
    ) -> None:
        target_by_field = {target.result_field: target for target in targets}
        comment_by_field = {fact.result_field: fact.comment for fact in comment_facts}
        seen_fields: set[str] = set()
        for rule_index, rule in enumerate(submission.translations):
            rule_path = f"translations.{rule_index}"
            target = target_by_field.get(rule.result_field)
            if target is None:
                raise TranslationToolValidationError(
                    code="rule_target_not_identified",
                    field_path=f"{rule_path}.result_field",
                    message=f"翻译规则字段 {rule.result_field} 未经节点 1 识别。",
                    repair_action=f"删除 {rule_path}；只能为 column_comments 中的结果字段生成规则。",
                )
            if rule.result_field in seen_fields:
                raise TranslationToolValidationError(
                    code="duplicate_translation_rule",
                    field_path=f"{rule_path}.result_field",
                    message=f"翻译规则字段 {rule.result_field} 被重复提交。",
                    repair_action=f"删除重复的 {rule_path}，只保留该字段第一次出现的规则。",
                )
            seen_fields.add(rule.result_field)
            if (rule.source_table, rule.source_field) != (
                target.source_table,
                target.source_field,
            ):
                raise TranslationToolValidationError(
                    code="translation_rule_lineage_mismatch",
                    field_path=rule_path,
                    message=(
                        f"翻译规则字段 {rule.result_field} 的来源必须是 "
                        f"{target.source_table}.{target.source_field}。"
                    ),
                    repair_action=(
                        f"将 {rule_path}.source_table 改为 {target.source_table}，"
                        f"并将 {rule_path}.source_field 改为 {target.source_field}。"
                    ),
                )
            if not rule.mappings:
                raise TranslationToolValidationError(
                    code="empty_translation_mappings",
                    field_path=f"{rule_path}.mappings",
                    message=f"翻译规则字段 {rule.result_field} 未提供任何映射。",
                    repair_action=(
                        f"从该字段 comment 中逐项提取映射并写入 {rule_path}.mappings；"
                        f"若无法可靠提取，则删除 {rule_path}。"
                    ),
                )
            comment = comment_by_field.get(rule.result_field)
            if not comment:
                raise TranslationToolValidationError(
                    code="missing_column_comment",
                    field_path=rule_path,
                    message=f"字段 {rule.result_field} 没有可用 comment。",
                    repair_action=f"删除 {rule_path}。",
                )
            raw_values: set[str] = set()
            for mapping_index, mapping in enumerate(rule.mappings):
                mapping_path = f"{rule_path}.mappings.{mapping_index}"
                if mapping.raw_value in raw_values:
                    raise TranslationToolValidationError(
                        code="duplicate_raw_mapping_value",
                        field_path=f"{mapping_path}.raw_value",
                        message=(
                            f"字段 {rule.result_field} 的原始值 "
                            f"{mapping.raw_value} 被重复定义。"
                        ),
                        repair_action=f"删除重复的 {mapping_path}。",
                    )
                raw_values.add(mapping.raw_value)
                if mapping.raw_value not in comment:
                    raise TranslationToolValidationError(
                        code="raw_value_not_in_comment",
                        field_path=f"{mapping_path}.raw_value",
                        message=(
                            f"原始值 {mapping.raw_value} 未出现在字段 "
                            f"{rule.result_field} 的 comment 中。"
                        ),
                        repair_action=f"删除 {mapping_path}；不得改写或猜测 comment 未定义的原始值。",
                    )
                if mapping.display_value not in comment:
                    raise TranslationToolValidationError(
                        code="display_value_not_in_comment",
                        field_path=f"{mapping_path}.display_value",
                        message=(
                            f"展示值 {mapping.display_value} 未出现在字段 "
                            f"{rule.result_field} 的 comment 中。"
                        ),
                        repair_action=f"删除 {mapping_path}；不得改写或猜测 comment 未定义的展示值。",
                    )

    # 为单个字段构造独立 YAML 并以 auto Function Calling 有限修复，避免其他字段注释污染映射。
    async def _build_translation_rule_for_field(
        self,
        target: TranslationTarget,
        comment_fact: ColumnCommentFact,
        rows: list[dict[str, Any]],
    ) -> tuple[list[ColumnTranslationRule], list[str]]:
        context = {
            "column_comments": [comment_fact.model_dump()],
            "observed_values": {
                target.result_field: list(
                    dict.fromkeys(
                        _normalize_mapping_key(row.get(target.result_field))
                        for row in rows[:RESULT_PREVIEW_ROW_COUNT]
                    )
                )
            },
        }
        messages: list[Any] = _build_translation_messages(
            TRANSLATION_RULE_SYSTEM_PROMPT,
            context,
            self._tool_tag_template,
        )
        tool = build_translation_rules_tool_definition(self._allowed_tables)
        raw_responses: list[str] = []
        last_error: Exception | None = None
        repair_context_start: int | None = None
        for attempt in range(MAX_TOOL_ARGUMENT_REPAIR_COUNT + 1):
            current_turn_start = len(messages)
            self._write_trace(
                f"[翻译层节点 2][{target.result_field}] "
                f"第 {attempt + 1} 次模型调用：解析字段 comment"
            )
            response = await create_traced_chat_completion(
                client=self._client,
                message_queue=self._message_trace_queue,
                node=f"translation.rule_generation.{target.result_field}",
                model=self._model,
                messages=[*messages],
                tools=[tool],
                tool_choice="auto",
                **self._request_profile.build_non_thinking_options(
                    self._max_tokens,
                ),
            )
            raw_responses.append(_serialize_raw_response(response))
            choice = response.choices[0]
            message = choice.message
            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                last_error = TranslationToolValidationError(
                    code=(
                        "translation_rule_tool_call_truncated"
                        if getattr(choice, "finish_reason", None) == "length"
                        else "translation_rule_tool_call_missing"
                    ),
                    field_path="$",
                    message=(
                        "本轮输出达到长度上限且没有形成可解析工具调用。"
                        if getattr(choice, "finish_reason", None) == "length"
                        else "本轮没有调用工具，普通文本不能提交字段翻译规则。"
                    ),
                    repair_action=(
                        "缩短映射内容，并只调用一次 submit_translation_rules。"
                        if getattr(choice, "finish_reason", None) == "length"
                        else "只调用一次 submit_translation_rules；comment 无法解析时提交 translations 空数组。"
                    ),
                )
                if attempt >= MAX_TOOL_ARGUMENT_REPAIR_COUNT:
                    break
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                messages.extend(
                    (
                        message,
                        _build_missing_tool_call_feedback_message(
                            SUBMIT_TRANSLATION_RULES_TOOL_NAME,
                            last_error,
                            self._tool_tag_template,
                        ),
                    )
                )
                self._write_trace(
                    f"[翻译层节点 2][{target.result_field}] 工具协议校验失败："
                    + messages[-1]["content"]
                )
                continue

            messages.append(message)
            if len(tool_calls) != 1:
                last_error = TranslationToolValidationError(
                    code="translation_rule_multiple_tool_calls",
                    field_path="$",
                    message=(
                        "单字段翻译规则每轮必须且只能调用一个工具，"
                        f"本轮实际调用 {len(tool_calls)} 个。"
                    ),
                    repair_action="重新生成本轮响应，并且只调用一次 submit_translation_rules。",
                )
                if attempt >= MAX_TOOL_ARGUMENT_REPAIR_COUNT:
                    break
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                messages.extend(
                    _build_protocol_tool_error_message(
                        tool_call.id,
                        tool_call.function.name,
                        SUBMIT_TRANSLATION_RULES_TOOL_NAME,
                        last_error.code,
                        str(last_error),
                    )
                    for tool_call in tool_calls
                )
                self._write_trace(
                    f"[翻译层节点 2][{target.result_field}] 工具数量校验失败："
                    + messages[-1]["content"]
                )
                continue

            tool_call = tool_calls[0]
            if tool_call.function.name != SUBMIT_TRANSLATION_RULES_TOOL_NAME:
                last_error = TranslationToolValidationError(
                    code="translation_rule_unexpected_tool",
                    field_path="$",
                    message=(
                        f"工具 {tool_call.function.name} 未在单字段规则节点注册，"
                        "本次调用未执行。"
                    ),
                    repair_action="只调用一次 submit_translation_rules。",
                )
                if attempt >= MAX_TOOL_ARGUMENT_REPAIR_COUNT:
                    break
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                messages.append(
                    _build_protocol_tool_error_message(
                        tool_call.id,
                        tool_call.function.name,
                        SUBMIT_TRANSLATION_RULES_TOOL_NAME,
                        last_error.code,
                        str(last_error),
                    )
                )
                self._write_trace(
                    f"[翻译层节点 2][{target.result_field}] 工具名称校验失败："
                    + messages[-1]["content"]
                )
                continue

            try:
                parsed = parse_translation_rules_tool_arguments(
                    tool_call.function.arguments,
                )
                self._validate_rules(parsed, [target], [comment_fact])
                if repair_context_start is not None:
                    self._write_trace(
                        f"[翻译层节点 2][{target.result_field}] 已修正工具调用；"
                        "节点结束后丢弃独立修复上下文，不会进入其他字段或后续节点"
                    )
                self._write_trace(
                    f"[翻译层节点 2][{target.result_field}] 已生成规则："
                    + (", ".join(item.result_field for item in parsed.translations) or "无")
                )
                return parsed.translations, raw_responses
            except (ValidationError, TranslationToolValidationError) as error:
                last_error = error
                if attempt >= MAX_TOOL_ARGUMENT_REPAIR_COUNT:
                    break
                if repair_context_start is None:
                    repair_context_start = current_turn_start
                if isinstance(error, ValidationError):
                    messages.append(
                        build_tool_argument_error_message(
                            tool_call.id,
                            SUBMIT_TRANSLATION_RULES_TOOL_NAME,
                            error,
                        )
                    )
                else:
                    messages.append(
                        _build_semantic_tool_error_message(
                            tool_call.id,
                            SUBMIT_TRANSLATION_RULES_TOOL_NAME,
                            error,
                        )
                    )
                self._write_trace(
                    f"[翻译层节点 2][{target.result_field}] 工具参数或语义校验失败："
                    + messages[-1]["content"]
                )
        raise RuntimeError(
            f"字段 {target.result_field} 的 comment 翻译规则生成失败：{last_error}"
        )

    # 在异步信号量内生成单字段规则，限制模型并发而不阻塞应用事件循环。
    async def _build_translation_rule_with_semaphore(
        self,
        semaphore: asyncio.Semaphore,
        target: TranslationTarget,
        comment_fact: ColumnCommentFact,
        rows: list[dict[str, Any]],
    ) -> tuple[list[ColumnTranslationRule], list[str]]:
        async with semaphore:
            return await self._build_translation_rule_for_field(
                target,
                comment_fact,
                rows,
            )

    # 节点 2 为每个字段单独解析 comment，并以受控异步并发复用相同系统提示和工具 Schema 前缀。
    async def _build_translation_rules(
        self,
        state: _ResultTranslationState,
    ) -> dict[str, Any]:
        comment_facts = self._build_comment_facts(
            state["targets"],
            state["schema_fields"],
        )
        if not comment_facts:
            self._write_trace("[翻译层节点 2] 没有可解析 comment，跳过模型调用")
            return {
                "comment_facts": [],
                "rules": [],
                "rule_generation_raw_responses": [],
            }

        targets_by_field = {
            target.result_field: target for target in state["targets"]
        }
        semaphore = asyncio.Semaphore(self._max_parallel_fields)
        generated_results = await asyncio.gather(
            *(
                self._build_translation_rule_with_semaphore(
                    semaphore,
                    targets_by_field[fact.result_field],
                    fact,
                    state["rows"],
                )
                for fact in comment_facts
            )
        )

        rules = [
            rule
            for field_rules, _ in generated_results
            for rule in field_rules
        ]
        raw_responses = [
            raw_response
            for _, field_responses in generated_results
            for raw_response in field_responses
        ]
        return {
            "comment_facts": comment_facts,
            "rules": rules,
            "rule_generation_raw_responses": raw_responses,
        }

    # 节点 3 对完整结果逐行应用规则，未定义值、空值和非目标字段原样保留。
    def _apply_translations(
        self,
        state: _ResultTranslationState,
    ) -> dict[str, Any]:
        mapping_by_field = {
            rule.result_field: {
                mapping.raw_value: mapping.display_value for mapping in rule.mappings
            }
            for rule in state["rules"]
        }
        translated_rows: list[dict[str, Any]] = []
        for row in state["rows"]:
            translated_row = dict(row)
            for result_field, field_mapping in mapping_by_field.items():
                original_value = row.get(result_field)
                translated_row[result_field] = field_mapping.get(
                    _normalize_mapping_key(original_value),
                    original_value,
                )
            translated_rows.append(translated_row)
        self._write_trace(
            f"[翻译层节点 3] 已对 {len(translated_rows)} 行完整结果执行确定性翻译"
        )
        return {"translated_rows": translated_rows}

    # 异步翻译最终塑形列；通过列来源映射追溯 SQL 原始别名，异常时完整保留塑形结果。
    async def run(
        self,
        sql: str,
        result_columns: list[str],
        rows: list[dict[str, Any]],
        schema_results: list[TableSchemaToolResponse] | None = None,
        result_column_sources: dict[str, str] | None = None,
    ) -> ResultTranslationSubgraphResult:
        if not sql.strip():
            raise ValueError("最终 SQL 不能为空")
        if not result_columns:
            raise ValueError("结果字段列表不能为空")
        try:
            if not rows:
                return ResultTranslationSubgraphResult(
                    original_rows=[],
                    translated_rows=[],
                )
            try:
                final_state = await self._workflow.ainvoke(
                    {
                        "sql": sql,
                        "result_columns": result_columns,
                        "rows": rows,
                        "result_column_sources": result_column_sources,
                        "provided_schema_results": schema_results,
                    }
                )
            except Exception as error:
                self._write_trace(f"[翻译层] 执行失败，保留原始结果：{error}")
                return ResultTranslationSubgraphResult(
                    status="failure",
                    original_rows=rows,
                    translated_rows=[dict(row) for row in rows],
                    error=str(error)[:500],
                )
            return ResultTranslationSubgraphResult(
                targets=final_state["targets"],
                comment_facts=final_state["comment_facts"],
                rules=final_state["rules"],
                original_rows=rows,
                translated_rows=final_state["translated_rows"],
                target_detection_raw_responses=final_state[
                    "target_detection_raw_responses"
                ],
                rule_generation_raw_responses=final_state[
                    "rule_generation_raw_responses"
                ],
            )
        finally:
            if self._close_client_after_run:
                await self._client.close()
